
"""
   Copyright 2018-2019 EPCC, University Of Edinburgh

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""

from flask_admin import Admin
from config import SECRET_KEY, SQLALCHEMY_DATABASE_URI, INPUT_STAGING_AREA, OUTPUT_STAGING_AREA, \
    INPUTSET_STAGING_AREA, MAX_USER_JOBS, REMOTE_JOB_STATE_REFRESH_PERIOD, APP_STATIC_URL, APP_LOGFILE, USE_WOS
from utils import queryresult_to_dict, queryresult_to_array, compute_hash_for_dir_contents
from flask_sqlalchemy import SQLAlchemy
from flask_security import Security, SQLAlchemyUserDatastore, \
    UserMixin, RoleMixin, login_required, utils
from flask_admin import helpers as admin_helpers
from flask_admin.contrib import sqla
from flask import Flask, url_for, redirect, request, abort
from wtforms import StringField, PasswordField
from flask_login import current_user
from flask_security.forms import RegisterForm
from sqlalchemy.sql import text
from flask import jsonify
import uuid
from flask_admin.contrib.sqla import ModelView
import os
import saga_utils
from flask import send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
from saga_utils import stage_output_files, cleanup_directory
from werkzeug.utils import secure_filename
import shutil
import re
from logging.config import dictConfig

from wos_utils import s3_upload, s3_list_files_for_job, get_presigned_url, s3_delete_files_for_job
import urllib3

# role definitions
SUPERUSER_ROLE = 'superuser'
POWERUSER_ROLE = 'poweruser'


scheduler = BackgroundScheduler()


dictConfig({
    'version': 1,
    'formatters': {'default': {
        'format': '[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
    }},
    'handlers': {
        'wsgi': {
        'class': 'logging.StreamHandler',
        'stream': 'ext://flask.logging.wsgi_errors_stream',
        'formatter': 'default'
    },
        'log_file_handler': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': APP_LOGFILE,
            'formatter': 'default',
            'maxBytes': 485760,
            'backupCount': 5,
            'encoding': 'utf8'
        },
    },
    'root': {
        'level': 'INFO',
        'handlers': ['wsgi', 'log_file_handler']
    }
})

# saga stuff can sometimes timeout on ssh, so increase the margin
os.environ['SAGA_PTY_SSH_TIMEOUT']='120'

app = Flask(__name__, static_url_path=APP_STATIC_URL)
app.config.from_pyfile('config.py')


admin = Admin(app, name='HemelB Offload Service', template_mode='bootstrap3', base_template='master.html')

# add database connection
app.config['SECRET_KEY'] = SECRET_KEY
app.config['SQLALCHEMY_DATABASE_URI'] = SQLALCHEMY_DATABASE_URI
db = SQLAlchemy(app)

# TODO:
# if we're using the WOS, should test the setup before we do anything else
if USE_WOS == True:
    app.logger.info("App is configured to use the Web Object Scalar")
else:
    app.logger.info("App is configured to use the local file system")

# possibly a bad idea, but don't want the log full of certificate warnings
urllib3.disable_warnings()



# Define models
roles_users = db.Table(
    'roles_users',
    db.Column('user_id', db.Integer(), db.ForeignKey('user.id')),
    db.Column('role_id', db.Integer(), db.ForeignKey('role.id'))
)


class Role(db.Model, RoleMixin):
    id = db.Column(db.Integer(), primary_key=True)
    name = db.Column(db.String(80), unique=True)
    description = db.Column(db.String(255))

    def __str__(self):
        return self.name


class User(UserMixin, db.Model):
    def __repr__(self):
        return self.first_name + " " + self.last_name

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(255))
    last_name = db.Column(db.String(255))
    email = db.Column(db.String(255), unique=True)
    password = db.Column(db.String(255))
    active = db.Column(db.Boolean())
    confirmed_at = db.Column(db.DateTime())
    roles = db.relationship('Role', secondary=roles_users,
                            backref=db.backref('users', lazy='dynamic'))


# quick model for testing jobs
class JobModel(db.Model):

    __tablename__ = 'JOB'

    id = db.Column(db.Integer(), primary_key=True)
    user_id = db.Column(db.Integer())
    local_job_id = db.Column(db.String(80), unique=True)
    remote_job_id = db.Column(db.String(80), unique=True)
    name = db.Column(db.String(80))
    executable = db.Column(db.String(255))
    state = db.Column(db.String(80))
    num_total_cpus = db.Column(db.Integer())
    total_physical_memory = db.Column(db.String(80))
    wallclock_limit = db.Column(db.String(80))
    project = db.Column(db.String(80))
    queue = db.Column(db.String(80))
    created = db.Column(db.Date())
    last_modified = db.Column(db.Date())




class ServiceModel(db.Model):
    __tablename__ = 'SERVICE'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)

    def __repr__(self):
        return self.name


# model for managing job templates
class JobTemplateModel(db.Model):

    __tablename__ = 'JOB_TEMPLATE'
    id = db.Column(db.Integer(), primary_key=True)
    service_id = db.Column(db.Integer(), db.ForeignKey('SERVICE.id'), nullable=False)
    service = db.relationship("ServiceModel", foreign_keys=[service_id])
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(80))
    executable = db.Column(db.String(255))
    num_total_cpus = db.Column(db.Integer())
    total_physical_memory = db.Column(db.String(80))
    wallclock_limit = db.Column(db.String(80))
    project = db.Column(db.String(80))
    queue = db.Column(db.String(80))
    arguments = db.Column(db.String(256))
    filter = db.Column(db.String(256))


    def __repr__(self):
        return self.name



class JobTemplateModelView(ModelView):


    # we need the current user's id for an insert
    def on_model_change(self, form, model, is_created):
        if is_created:
            model.user_id = current_user.id

    def is_accessible(self):
        if current_user.has_role(SUPERUSER_ROLE):
            return True
        else:
            return False




class ReadOnlyModelView(ModelView):

    can_create = False
    can_edit = False
    can_delete = False

    def is_accessible(self):
        if not current_user.is_active or not current_user.is_authenticated:
            return False
        return True

    def inaccessible_callback(self, name, **kwargs):
        # redirect to login page if user doesn't have access
        return redirect(url_for('security.login', next=request.url))



    def __repr__(self):
        return self.name

# Setup Flask-Security
user_datastore = SQLAlchemyUserDatastore(db, User, Role)

class ExtendedRegisterForm(RegisterForm):
    first_name = StringField('First Name')
    last_name = StringField('Last Name')

security = Security(app, user_datastore, register_form=ExtendedRegisterForm)

# define a context processor for merging flask-admin's template context into the
# flask-security views.
@security.context_processor
def security_context_processor():
    return dict(
        admin_base_template=admin.base_template,
        admin_view=admin.index_view,
        h=admin_helpers,
        get_url=url_for
    )

# Create customized model view class
class UserModelView(sqla.ModelView):

    column_list = ('first_name', 'last_name', 'email')
    column_searchable_list = ('first_name', 'last_name', 'email')
    column_exclude_list = ('password')
    form_excluded_columns = ('password')

    can_create = True
    can_delete = False

    def is_accessible(self):
        if not current_user.is_active or not current_user.is_authenticated:
            return False
        if current_user.has_role('superuser'):
            return True
        return False

    def _handle_view(self, name, **kwargs):
        if not self.is_accessible():
            if current_user.is_authenticated:
                # permission denied
                abort(403)
            else:
                # login
                return redirect(url_for('security.login', next=request.url))

                # On the form for creating or editing a User, don't display a field corresponding to the model's password field.
                # There are two reasons for this. First, we want to encrypt the password before storing in the database. Second,
                # we want to use a password field (with the input masked) rather than a regular text field.

    def scaffold_form(self):

        # Start with the standard form as provided by Flask-Admin. We've already told Flask-Admin to exclude the
        # password field from this form.
        form_class = super(UserModelView, self).scaffold_form()

        # Add a password field, naming it "password2" and labeling it "New Password".
        form_class.password2 = PasswordField('New Password')
        return form_class

        # This callback executes when the user saves changes to a newly-created or edited User -- before the changes are
        # committed to the database.

    def on_model_change(self, form, model, is_created):

        # If the password field isn't blank...
        if len(model.password2):
            # ... then encrypt the new password prior to storing it in the database. If the password field is blank,
            # the existing password in the database will be retained.
            # TODO - put a password strength checker here?
            model.password = utils.hash_password(model.password2)


class RoleModelView(sqla.ModelView):

        def is_accessible(self):
            if not current_user.is_active or not current_user.is_authenticated:
                return False
            if current_user.has_role('superuser'):
                return True
            return False

        def _handle_view(self, name, **kwargs):
            if not self.is_accessible():
                if current_user.is_authenticated:
                    # permission denied
                    abort(403)
                else:
                    # login
                    return redirect(url_for('security.login', next=request.url))






# Add administrative views here
admin.add_view(RoleModelView(Role, db.session))
admin.add_view(UserModelView(User, db.session))
admin.add_view(ReadOnlyModelView(JobModel, db.session))
admin.add_view(JobTemplateModelView(JobTemplateModel, db.session))


# rest endpoint definitions
@app.route('/jobs')
@login_required
def list_jobs():
    # normal users can only see their own jobs
    # super users and power users can see all jobs
    result = None
    if current_user.has_role(SUPERUSER_ROLE) or current_user.has_role(POWERUSER_ROLE):
        cmd = 'SELECT local_job_id, name, state FROM JOB ORDER BY created'
        result = db.engine.execute(text(cmd))
    else:
        cmd = 'SELECT local_job_id, name, state FROM JOB WHERE user_id = :user_id'
        result = db.engine.execute(text(cmd), user_id=current_user.get_id())

    return jsonify(queryresult_to_array({'local_job_id','name','state'}, result))

@app.route('/jobs',  methods=['POST'])
@login_required
def create_new_job():

    # check the user is within their limit
    cmd = "SELECT COUNT(id) AS TOTAL_JOBS FROM JOB WHERE user_id=:user_id and state!='DELETED'"
    user_id = current_user.get_id()
    result = db.engine.execute(text(cmd), user_id=user_id)
    total_jobs = int(result.fetchone()['TOTAL_JOBS'])
    if total_jobs >= MAX_USER_JOBS:
        abort(500, "Maximum number of user jobs exceeded - delete some jobs")

    # look for json job description in payload

    payload = request.json

    job_spec = {

        'job_name': None,
        'service_id': None,
        'executable': None,
        'arguments': None,
        'env': None,
        'num_total_cpus': None,
        'total_physical_memory': None,
        'wallclock_limit': None,
        'project': None,
        'queue': None,
        'extended': None,
        'filter': None,
        'input_set_id': None
    }


    # if a template has been specified we take all our parameters from the template record and ignore everything else
    template_name = payload.get('template_name')
    if template_name is not None:

        cmd = "SELECT * FROM JOB_TEMPLATE WHERE name=:name"
        result = db.engine.execute(text(cmd), name=template_name).fetchone()
        if result is None:
            abort(404, "No matching job template found for name " + template_name)

        job_spec['job_name'] = result['name']
        job_spec['service_id'] = result['service_id']
        job_spec['executable'] = result['executable']
        job_spec['arguments'] = result['arguments']
        job_spec['env'] = result['env']
        job_spec['num_total_cpus'] = result['num_total_cpus']
        job_spec['total_physical_memory'] = result['total_physical_memory']
        job_spec['wallclock_limit'] = result['wallclock_limit']
        job_spec['project'] = result['project']
        job_spec['queue'] = result['queue']
        job_spec['extended'] = result['extended']
        job_spec['filter'] = result['filter']
        job_spec['input_set_id'] = result['input_set_id']


    # now look at the rest of the payload; user-supplied arguments override template arguments

    if 'arguments' in payload:
        arguments = payload.get('arguments')
        job_spec['arguments'] = arguments


    # for security reasons only powerusers or admins may override other aspects of the job
    # otherwise, any additional information in the payload is ignored

    if current_user.has_role(SUPERUSER_ROLE) or current_user.has_role(POWERUSER_ROLE):

        if 'name' in payload: job_spec['job_name'] = payload['name']

        if 'executable' in payload: job_spec['executable'] = payload['executable']

        if 'service' in payload:
            cmd = 'SELECT id FROM SERVICE where name=:name'
            result = db.engine.execute(text(cmd), name=payload['service'])
            job_spec['service_id'] = result.fetchone()['id']

        # look for additional information, will set as NULL if not in payload

        if 'num_total_cpus' in payload: job_spec['num_total_cpus'] = payload.get('num_total_cpus')
        if 'total_physical_memory' in payload: job_spec['total_physical_memory'] = payload.get('total_physical_memory')
        if 'wallclock_limit' in payload: job_spec['wallclock_limit'] = payload.get('wallclock_limit')
        if 'project' in payload: job_spec['project'] = payload.get('project')
        if 'queue' in payload: job_spec['queue'] = payload.get('queue')
        if 'extended' in payload: job_spec['extended'] = payload.get('extended')
        if 'input_set_id' in payload: job_spec['input_set_id'] = payload.get('input_set_id')

        if 'filter' in payload:
            job_spec['filter'] = payload.get('filter')


    # sanity check the filter rather than have it fail later
    if job_spec['filter'] is not None:
        try:
            re.compile(job_spec['filter'])
        except Exception as e:
            app.logger.error(e.message)
            abort(500, "Invalid filter specification: " + e.message)


    # sanity check env - must be able to turn it into a dict
    if 'env' in payload:
        env = payload.get('env')
        try:
            env_list = [str(s) for s in env.split()]
            env_dict = {}
            for p in env_list:
                q = p.split("=")
                env_dict[q[0]] = q[1]
            job_spec['env'] = env
        except Exception as e:
            abort(500, "Invalid env specification: " + e.message)



    job_uuid = str(uuid.uuid4())

    # look up the service name to get the correct id


    user_id = current_user.get_id()

    cmd = 'INSERT INTO JOB(user_id, name, executable, service_id, local_job_id, arguments, num_total_cpus, ' \
          'total_physical_memory, wallclock_limit, project, queue, filter, extended, env) \
        VALUES(:user_id, :name, :executable, :service_id, :local_job_id, :arguments, ' \
          ':num_total_cpus, :total_physical_memory, :wallclock_limit, :project, :queue, :filter, :extended, :env )'
    db.engine.execute(text(cmd), user_id=user_id, name=job_spec['job_name'], executable=job_spec['executable'],
                      service_id=job_spec['service_id'], local_job_id=job_uuid, arguments=job_spec['arguments'],
                      num_total_cpus=job_spec['num_total_cpus'], total_physical_memory=job_spec['total_physical_memory'],
                      wallclock_limit=job_spec['wallclock_limit'], project=job_spec['project'], queue=job_spec['queue'],
                      filter=job_spec['filter'], extended=job_spec['extended'], env=job_spec['env'])

    # create a staging area for this job
    try:
        os.mkdir(os.path.join(INPUT_STAGING_AREA, job_uuid))
    except Exception as e:
        app.logger.error(e.message)
        abort(500, e.message)


    return str(job_uuid)

@app.route('/jobs/<id>/state',  methods=['GET'])
@login_required
def get_job_state(id):
    # normal users can only see information about jobs they own
    # power and superusers can see everything
    cmd = "SELECT state, user_id FROM JOB WHERE local_job_id=:local_job_id"
    result = db.engine.execute(text(cmd), local_job_id = id).fetchone()
    if result is None:
        abort(404)

    state = result['state']
    owner = result['user_id']

    if int(owner) != int(current_user.get_id()):
        if not (current_user.has_role(POWERUSER_ROLE) or current_user.has_role(SUPERUSER_ROLE)):
            abort(403)

    return state, 200, {'Content-Type': 'text/plain'}



@app.route('/templates',  methods=['GET'])
@login_required
def get_job_templates():
    # normal users can only see information about jobs they own
    # power and superusers can see everything
    cmd = "SELECT name, id, description FROM JOB_TEMPLATE ORDER BY name"
    result = db.engine.execute(text(cmd))
    if result is None:
        abort(404)

    return jsonify(queryresult_to_array({'name', 'id', 'description'}, result))








@app.route('/jobs/<id>/retrieved',  methods=['GET'])
@login_required
def get_job_retrieved(id):
    # normal users can only see information about jobs they own
    # power and superusers can see everything
    cmd = "SELECT user_id, retrieved FROM JOB WHERE local_job_id=:local_job_id"
    result = db.engine.execute(text(cmd), local_job_id = id).fetchone()
    if result is None:
        abort(404)

    retrieved = result['retrieved']
    owner = result['user_id']

    if int(owner) != int(current_user.get_id()):
        if not (current_user.has_role(POWERUSER_ROLE) or current_user.has_role(SUPERUSER_ROLE)):
            abort(403)

    return str(retrieved), 200, {'Content-Type': 'text/plain'}


@app.route('/jobs/<id>',  methods=['GET'])
@login_required
def get_job_description(id):
    # normal users can only see information about jobs they own
    # power and superusers can see everything

    cmd = "SELECT * FROM JOB WHERE local_job_id=:local_job_id"
    result = db.engine.execute(text(cmd), local_job_id = id)
    job_record = result.fetchone()
    if job_record is None:
        abort(404)

    if int(job_record['user_id']) != int(current_user.get_id()):
        if not (current_user.has_role(POWERUSER_ROLE) or current_user.has_role(SUPERUSER_ROLE)):
            abort(403)


    jd = {}
    jd["name"] = job_record['name']
    jd["executable"] = job_record['executable']
    jd["service_id"] = job_record['service_id']
    jd["local_job_id"] = job_record["local_job_id"]
    jd["remote_job_id"] = job_record["remote_job_id"]
    jd["arguments"] = job_record['arguments']
    #jd["env"] = job_record['env']
    jd["num_total_cpus"] = job_record["num_total_cpus"]
    jd["total_physical_memory"] = job_record['total_physical_memory']
    jd["wallclock_limit"] = job_record['wallclock_limit']
    jd["project"] = job_record["project"]
    jd["queue"] = job_record["queue"]
    jd['input_set_id'] = job_record['input_set_id']
    jd['filter'] = job_record['filter']

    return jsonify(jd)


def submit_job(id):

    cmd = "SELECT * FROM JOB WHERE local_job_id=:local_job_id"
    result = db.engine.execute(text(cmd), local_job_id=id).fetchone()

    if result is None:
        app.logger.error("Error submitting job, job not found")
        return

    if result['state'] != "NEW":
        app.logger.error("Error submitting job, inconsistent state")
        return

    jd = {}
    jd['local_job_id'] = result['local_job_id']
    jd['name'] = result['name']
    jd['service_id'] = result['service_id']
    jd['executable'] = result['executable']

    # look for additional information, will set as NULL if not in payload

    if result['arguments'] is not None:
        arguments = result['arguments']
        arguments_list =  [str(s) for s in arguments.split()]
        jd['arguments'] = arguments_list

    jd['num_total_cpus'] = result['num_total_cpus']
    jd['total_physical_memory'] = result['total_physical_memory']
    jd['wallclock_limit'] = result['wallclock_limit']
    jd['project'] = result['project']
    jd['queue'] = result['queue']
    jd['extended'] = result['extended']

    # if env is specified, we need to turn the arguments into a dict
    # we expect the string to take the form name=value name=value etc
    if result['env'] is not None:
        try:
            env = result['env']
            env_list = [str(s) for s in env.split()]
            env_dict = {}
            for p in env_list:
                q = p.split("=")
                env_dict[q[0]] = q[1]
            jd['environment'] = env_dict
        except Exception as e:
            app.logger.error(e.message)


    cmd = "UPDATE JOB SET state=:state WHERE local_job_id=:local_job_id"
    db.engine.execute(text(cmd), state="STAGING", local_job_id=id)

    local_input_file_dir = os.path.join(INPUT_STAGING_AREA, jd['local_job_id'])

    service_id = result['service_id']
    service = get_service(service_id)

    if result['input_set_id'] is not None:
        input_set_dir = os.path.join(INPUTSET_STAGING_AREA, result['input_set_id'])
        try:
            saga_utils.stage_input_files(id, input_set_dir, service)
        except Exception as e:
            app.logger.error(e.message)
            cmd = "UPDATE JOB SET state=:state WHERE local_job_id=:local_job_id"
            db.engine.execute(text(cmd), state="STAGING_FAILED", local_job_id=id)
            return

    # stage any input files uploaded for this job
    try:
        saga_utils.stage_input_files(id, local_input_file_dir, service)
    except Exception as e:
        app.logger.error(e.message)
        cmd = "UPDATE JOB SET state=:state WHERE local_job_id=:local_job_id"
        db.engine.execute(text(cmd), state="STAGING_FAILED", local_job_id=id)
        return

    # kick off the job and get an id for tracking the remote job state
    remote_job_id = None
    try:
        remote_job_id = saga_utils.submit_saga_job(jd, service)
    except Exception as e:
        app.logger.error(e.message)
        cmd = "UPDATE JOB SET state=:state WHERE local_job_id=:local_job_id"
        db.engine.execute(text(cmd), state="FAILED", local_job_id=id)
        return

    if remote_job_id != -1:
        # update database
        cmd = "UPDATE JOB SET state=:state, remote_job_id=:remote_job_id WHERE local_job_id=:local_job_id"
        db.engine.execute(text(cmd), state="SUBMITTED", remote_job_id=remote_job_id, local_job_id=id)
    else:
        cmd = "UPDATE JOB SET state=:state WHERE local_job_id=:local_job_id"
        db.engine.execute(text(cmd), state="FAILED", local_job_id=id)


@app.route('/jobs/<id>/submit',  methods=['POST'])
@login_required
def handle_submit_job(id):

    # only the owner of a job can submit it

    cmd = "SELECT * FROM JOB WHERE local_job_id=:local_job_id"
    result = db.engine.execute(text(cmd), local_job_id=id).fetchone()

    if result is None:
        abort(404)

    if(int(result['user_id']) != int(current_user.get_id())):
        abort(403)

    if result['state'] != "NEW":
        abort("inconsistent state", 500)

    scheduler.add_job(submit_job, args=[id])

    return 'Success', 200, {'Content-Type': 'text/plain'}



@app.route('/jobs/<id>/files',  methods=['POST'])
@login_required
def add_file_to_job(id):
    # first check the job exists
    cmd = "SELECT local_job_id, user_id FROM JOB WHERE local_job_id=:local_job_id"
    result = db.engine.execute(text(cmd), local_job_id=id)
    r = result.fetchone()
    if r is None:
        abort(404)
    local_job_id = r['local_job_id']
    user_id = r['user_id']
    if int(r['user_id']) != int(current_user.get_id()):
        abort(403)

    print("Uploading files for job " + local_job_id)

    for f in request.files:
        file = request.files[f]
        print(f)
        file_path = os.path.join(INPUT_STAGING_AREA, local_job_id, secure_filename(f))
        print("upload path is " + file_path)
        file.save(file_path)
        try:
            print(os.path.getsize(file_path))
        except Exception as e:
            print(e)

    return 'Success', 200, {'Content-Type': 'text/plain'}

@app.route('/jobs/<id>/files',  methods=['GET'])
@login_required
def get_job_output_file_list(id):

    # quickly check if the job id is real
    cmd = "SELECT local_job_id, user_id FROM JOB WHERE local_job_id=:local_job_id"
    result = db.engine.execute(text(cmd), local_job_id=id)
    r = result.fetchone()
    if r is None:
        abort(404)
    local_job_id = r['local_job_id']
    user_id = r['user_id']

    # normal users can only see their own jobs
    if int(user_id) != int(current_user.get_id()):
        if not ( current_user.has_role(SUPERUSER_ROLE) or current_user.has_role(POWERUSER_ROLE)):
            abort(403)

    # list the files in the job's output directory
    # in the case of Azure we would list the output storage container

    #filelist = []
    #base_dir = os.path.join(OUTPUT_STAGING_AREA, local_job_id)
    #for path, subdirs, files in os.walk(base_dir):
    #    for name in files:
    #        filelist.append( os.path.join(path, name).replace(base_dir+"/",''))


    # list the files from the WOS
    if USE_WOS == True:
        filelist = s3_list_files_for_job(local_job_id)
        return jsonify(filelist)
    else:
        filelist = []
        base_dir = os.path.join(OUTPUT_STAGING_AREA, local_job_id)
        for path, subdirs, files in os.walk(base_dir):
            for name in files:
                filelist.append( os.path.join(path, name).replace(base_dir+"/",''))
                return jsonify(filelist)




@app.route('/jobs/<job_id>/files/<path:path>',  methods=['GET'])
@login_required
def get_job_output_file(job_id, path):

    # quickly check if the job id is real
    cmd = "SELECT local_job_id, user_id FROM JOB WHERE local_job_id=:local_job_id"
    result = db.engine.execute(text(cmd), local_job_id=job_id)
    r = result.fetchone()
    if r is None:
        abort(404)
    local_job_id = r['local_job_id']
    user_id = r['user_id']

    if int(user_id) != int(current_user.get_id()):
        abort(403)

    # check if the requested file exists
    # we need a check to see if we are listing normal files or redirecting to WOS

    if USE_WOS == True:
        key_path = os.path.join(job_id, path)
        url = get_presigned_url(key_path)
        return redirect(url, code=302)
    else:
        exists = os.path.isfile(os.path.join(OUTPUT_STAGING_AREA, local_job_id, path))
        if not exists:
           abort(404)

    return send_from_directory(directory=os.path.join(OUTPUT_STAGING_AREA, local_job_id), filename=path)


@app.route('/jobs/<id>', methods=['DELETE'])
@login_required
def delete_job(id):

    try:

        # check ownership of the job
        cmd = "SELECT user_id, service_id, remote_job_id, state, retrieved FROM JOB WHERE local_job_id = :local_job_id"
        result = db.engine.execute(text(cmd), local_job_id=id)
        r = result.fetchone()
        if r is None:
            return "Resource not found", 404, {'Content-Type': 'text/plain'}

        if int(r['user_id']) != int(current_user.get_id()):
            if not ( current_user.has_role(SUPERUSER_ROLE)):
                return "Permission Denied", 403, {'Content-Type': 'text/plain'}


        # if the job is already set as deleted, ignore this request
        if r['state'] == 'DELETED':
            return 200

        service = get_service(r['service_id'])

        # only do remote cleanup if not already done
        if r['retrieved'] != 1:



            # kill the remote job if still running

            remote_job_id = r['remote_job_id']

            if remote_job_id is not None:
                try:
                    saga_utils.cancel_job(remote_job_id, service)
                except Exception as e:
                    app.logger.error("SAGA: error cancelling job:" + e.message)

            # delete any remote files associated with this job
            REMOTE_WORKING_DIR = os.path.join(service['working_directory'], str(id))
            try:
                cleanup_directory(REMOTE_WORKING_DIR, service)
            except Exception as e:
                app.logger.error("SAGA: error cleaning up directory:" + e.message)

        # delete any retrieved output files associated with the job

        LOCAL_OUTPUT_DIR = os.path.join(OUTPUT_STAGING_AREA, str(id))
        if os.path.exists(LOCAL_OUTPUT_DIR):
            shutil.rmtree(LOCAL_OUTPUT_DIR)

        # delete any local input files associated with the job
        LOCAL_INPUT_DIR = os.path.join(INPUT_STAGING_AREA, str(id))
        if os.path.exists(LOCAL_INPUT_DIR):
            shutil.rmtree(LOCAL_INPUT_DIR)

        # delete any files on the WOS
        if USE_WOS == True:
            s3_delete_files_for_job(id)


        # update the job to show as deleted
        cmd = "UPDATE JOB SET state=:state WHERE local_job_id = :local_job_id"
        result = db.engine.execute(text(cmd), state="DELETED", local_job_id=id)

        return "Deleted", 200, {'Content-Type': 'text/plain'}

    except Exception as e:
        abort(500, e.message)


@app.route('/services', methods=['GET'])
@login_required
def list_resources():
    cmd = 'SELECT name, scheduler_url, file_url FROM SERVICE'
    result = db.engine.execute(text(cmd))
    return jsonify(queryresult_to_dict({'name', 'scheduler_url', 'file_url'}, result))


@app.route('/inputsets', methods=['POST'])
@login_required
def create_input_set():

    try:
        # request must contain a name
        name = request.form.get("name", None)
        if name is None:
            abort(500, "request must contain a name")

        # name must be unique, do an explicit check
        cmd = "SELECT id FROM INPUT_SET WHERE name=:name"
        result = db.engine.execute(text(cmd), name=name)
        r = result.fetchone()
        if r is not None:
            abort(500, "name already in use")

        user_id = current_user.get_id()

        cmd = "INSERT INTO INPUT_SET (user_id, name) VALUES(:user_id, :name)"
        db.engine.execute(text(cmd), user_id=user_id, name=name)

        # return the id of the new record
        cmd = "SELECT id FROM INPUT_SET WHERE name=:name"
        result = db.engine.execute(text(cmd), name=name)

        r = result.fetchone()
        if r is None:
            abort(500)

        id = r['id']
        return str(id), 200, {'Content-Type': 'text/plain'}

    except Exception as e:
        abort(500, e.message)



@app.route('/inputsets', methods=['GET'])
@login_required
def list_input_sets():
    cmd = 'SELECT name, id FROM INPUT_SET'
    result = db.engine.execute(text(cmd))
    return jsonify(queryresult_to_array({'name', 'id'}, result))



@app.route('/inputsets/<id>/hash', methods=['GET'])
@login_required
def get_inputset_hash(id):

    # check the input set exists
    cmd = "SELECT id FROM INPUT_SET WHERE id=:id"
    result = db.engine.execute(text(cmd), id=id)
    r = result.fetchone()
    if r is None:
        abort(404)

    # check the input set directory exists
    dir = os.path.join(INPUTSET_STAGING_AREA, id)
    if not os.path.exists(dir):
        abort(404, "input set directory does not exist")

    hash = compute_hash_for_dir_contents(dir)

    return hash, 200, {'Content-Type': 'text/plain'}



@app.route('/inputsets/<id>/files',  methods=['POST'])
@login_required
def add_file_to_inputset(id):
    # first check the input set exists
    cmd = "SELECT id, user_id FROM INPUT_SET WHERE id=:id"
    result = db.engine.execute(text(cmd), id=id)
    r = result.fetchone()
    if r is None:
        abort(404)

    user_id = r['user_id']

    if int(user_id) != int(current_user.get_id()):
        if not ( current_user.has_role(SUPERUSER_ROLE) or current_user.has_role(POWERUSER_ROLE)):
            abort(403)

    # check the directory exists for the input set
    file_dir = os.path.join(INPUTSET_STAGING_AREA, id)
    if not os.path.exists(file_dir):
        os.makedirs(file_dir)

    for f in request.files:
        file = request.files[f]
        file.save(os.path.join(file_dir, secure_filename(f)))

    return 'Success', 200, {'Content-Type': 'text/plain'}

@app.route('/inputsets/<id>/files',  methods=['GET'])
@login_required
def get_inputset_file_list(id):

    # quickly check if the inputset id is real
    cmd = "SELECT id, user_id FROM INPUT_SET WHERE id=:id"
    result = db.engine.execute(text(cmd), id=id)
    r = result.fetchone()
    if r is None:
        abort(404)
    user_id = r['user_id']

    # normal users can only see their own assets
    if int(user_id) != int(current_user.get_id()):
        if not ( current_user.has_role(SUPERUSER_ROLE) or current_user.has_role(POWERUSER_ROLE)):
            abort(403)

    # list the files in the job's output directory
    # in the case of Azure we would list the output storage container

    filelist = []
    base_dir = os.path.join(INPUTSET_STAGING_AREA, id)

    if not os.path.exists(base_dir):
        abort(404)

    for path, subdirs, files in os.walk(base_dir):
        for name in files:
            filelist.append( os.path.join(path, name).replace(base_dir+"/",''))


    return jsonify(filelist)


@app.route('/inputsets/<id>/files/<filename>',  methods=['DELETE'])
@login_required
def delete_inputset_file(id, filename):

    # quickly check if the inputset id is real
    cmd = "SELECT id, user_id FROM INPUT_SET WHERE id=:id"
    result = db.engine.execute(text(cmd), id=id)
    r = result.fetchone()
    if r is None:
        abort(404)
    user_id = r['user_id']

    # normal users can only see their own assets
    if int(user_id) != int(current_user.get_id()):
        if not ( current_user.has_role(SUPERUSER_ROLE) or current_user.has_role(POWERUSER_ROLE)):
            abort(403)

    # delete the file if it exists, otherwise return a not found
    filepath = os.path.join(INPUTSET_STAGING_AREA, id, secure_filename(filename))
    if os.path.exists(filepath):
        os.remove(filepath)
        return 'Deleted', 200, {'Content-Type': 'text/plain'}
    else:
        abort(404)



@app.route('/inputsets/<id>',  methods=['DELETE'])
@login_required
def delete_inputset(id):

    # quickly check if the inputset id is real
    cmd = "SELECT id, user_id FROM INPUT_SET WHERE id=:id"
    result = db.engine.execute(text(cmd), id=id)
    r = result.fetchone()
    if r is None:
        abort(404)
    user_id = r['user_id']

    # normal users can only see their own assets
    if int(user_id) != int(current_user.get_id()):
        if not ( current_user.has_role(SUPERUSER_ROLE) or current_user.has_role(POWERUSER_ROLE)):
            abort(403)


    # delete the inputset directory if it exists, otherwise return a not found
    dirpath = os.path.join(INPUTSET_STAGING_AREA, id)
    if os.path.exists(dirpath):
        shutil.rmtree(dirpath)
        return 'Deleted', 200, {'Content-Type': 'text/plain'}
    else:
        abort(404)





# refresh the local job state for any jobs with a local state of SUBMITTED
# do this on some kind of background thread?
def refresh_job_state():

    try:

        cmd = "SELECT local_job_id, remote_job_id, service_id FROM JOB WHERE state='SUBMITTED'"
        result = db.engine.execute(text(cmd))
        for r in result:

            try:
                local_job_id = r['local_job_id']
                remote_job_id = r['remote_job_id']

                service = get_service(r['service_id'])
                remote_state = None

                try:
                    remote_state = saga_utils.get_remote_job_state(remote_job_id, service)
                except Exception as e:
                    app.logger.error("refresh_job_state 1:" + e.message)

                if (remote_state in ['Done', 'DONE', 'Failed', 'FAILED']):
                    # update the local state
                    cmd = "UPDATE JOB SET state=:state WHERE local_job_id=:local_job_id"
                    db.engine.execute(text(cmd), state=remote_state, local_job_id=local_job_id)

                    try:
                        scheduler.add_job(retrieve_output_files, args=[local_job_id])
                    except Exception as e:
                        app.logger.error(e.message)

            except Exception as e:
                app.logger.error(e.message)

    except Exception as e:
        app.logger.error(e.message)


def get_service(service_id):
    cmd = "SELECT * FROM SERVICE WHERE id=:service_id"
    result = db.engine.execute(text(cmd), service_id=service_id).fetchone()

    service = {}
    service["name"] = result["name"]
    service["scheduler_url"] = result["scheduler_url"]
    service["username"] = result["username"]
    service["user_pass"] = result["user_pass"]
    service["user_key"] = result["user_key"]
    service["file_url"] = result["file_url"]
    service["working_directory"] = result["working_directory"]

    return service


def retrieve_output_files(job_id):
    cmd = "SELECT remote_job_id, service_id, filter FROM JOB WHERE local_job_id=:local_job_id"
    result = db.engine.execute(text(cmd), local_job_id=job_id)
    job = result.fetchone()

    local_file_dir = os.path.join(OUTPUT_STAGING_AREA, job_id)

    service = get_service(job['service_id'])

    try:
        REMOTE_WORKING_DIR = os.path.join(service['working_directory'], str(job_id))
        filter = job['filter']
        try:
            log_message = "Staging output files: " + REMOTE_WORKING_DIR + "," + local_file_dir
            app.logger.info(log_message)
            stage_output_files(REMOTE_WORKING_DIR, local_file_dir, service, filter, app.logger)
            app.logger.info("Staging complete")

            cleanup_directory(REMOTE_WORKING_DIR, service)

            # copy the local files to the WOS
            if USE_WOS == True:
                try:
                    copy_local_files_to_s3(local_file_dir, job_id)
                    # delete the local files once they have been copied to the WOS
                    shutil.rmtree(local_file_dir)
                except Exception as e:
                    app.logger.error(e.message())




            # force delete the remote working directory, saga doesn't currently do nested subdirs
            #try:
            #    scheduler_url = service["scheduler_url"]
            #    index = scheduler_url.find('//')
            #    server_name = scheduler_url[index + 2:]
            #    cmd = "rm -rf " + REMOTE_WORKING_DIR
            #    stdout, stderr = run_remote_command(server_name, service['username'], service['user_pass'], cmd)

            #except Exception as e:
            #    app.logger.message("retrieve_output_files:" + e.message)



        except Exception as e:
            app.logger.error("retrieve_output_files:" + e.message)

        # flag the job as retrieved
        cmd = "UPDATE JOB SET retrieved=1 WHERE local_job_id=:local_job_id"
        db.engine.execute(text(cmd), local_job_id=job_id)

    except Exception as e:
        app.logger.error("retrieve_output_files:" + e.message)


def copy_local_files_to_s3(local_job_dir, parent_dir):

    for root, dir, files in os.walk(local_job_dir):
        for f in files:
            filename = os.path.join(root, f)
            keyname = os.path.join(parent_dir, os.path.relpath(filename, local_job_dir))
            s3_upload(filename, keyname)



scheduler.add_job(refresh_job_state, 'interval', minutes=REMOTE_JOB_STATE_REFRESH_PERIOD)
scheduler.start()

if __name__ == '__main__':
    app.run()