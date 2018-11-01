from __future__ import print_function
import saga
import os




# name for the job's stdout file
JOB_STDOUT = "mixed.stdout"

# name for the job's stderr file
JOB_STDERR = "mixed.stderr"

# name for the job's output file, as opposed to stdout
JOB_OUTPUT_FILE = "output_file.txt"



def get_remote_job_state(remote_job_id, service):

    try:
        ctx = saga.Context("UserPass")
        ctx.user_id = service['username']
        ctx.user_pass = service['user_pass']

        # create a session and pass our context
        session = saga.Session()
        session.add_context(ctx)

        js = saga.job.Service(service['scheduler_url'], session)
        job = js.get_job(remote_job_id)
        state = job.state
        js.close()
        return state
    except saga.SagaException as ex:
        # Catch all saga exceptions
        print('An exception occured: {0} {1}'.format(ex.type, ex))
        # Trace back the exception. That can be helpful for debugging.
        print('Backtrace: {}'.format(ex.traceback))
        return None




def stage_input_files(job_id, local_input_file_dir, service):
    try:

        # create an SSH context and populate it with our SSH details.


        ctx = saga.Context("UserPass")
        ctx.user_id = service['username']
        ctx.user_pass = service['user_pass']

        # create a session and pass our context
        session = saga.Session()
        session.add_context(ctx)

        # specify a new working directory for the job
        # we use a UID here, but any unique identifer would work

        REMOTE_WORKING_DIR = service['working_directory']

        # create the job's working directory and copy over the contents of our input directory

        dir = saga.filesystem.Directory(service['file_url'] + REMOTE_WORKING_DIR, session=session)
        dir.make_dir(str(job_id))


        for f in os.listdir(local_input_file_dir):
            transfertarget = service['file_url'] + REMOTE_WORKING_DIR + "/" + str(job_id) + "/" + f
            transfersource = os.path.join(local_input_file_dir, f)
            out = saga.filesystem.File(transfersource, session=session)
            out.copy(transfertarget)

        dir.close()
        return 0

    except saga.SagaException as ex:
        # Catch all saga exceptions
        print('An exception occured: {0} {1}'.format(ex.type, ex))
        # Trace back the exception. That can be helpful for debugging.
        print('Backtrace: {}'.format(ex.traceback))
        return -1



def submit_saga_job(job_description, service):
    try:

        # create an SSH context and populate it with our SSH details.

        ctx = saga.Context("UserPass")
        ctx.user_id = service['username']
        ctx.user_pass = service['user_pass']

        # create a session and pass our context
        session = saga.Session()
        session.add_context(ctx)

        # specify a new working directory for the job
        # we use a UID here, but any unique identifer would work

        REMOTE_WORKING_DIR = os.path.join(service['working_directory'], str(job_description['local_job_id']))


        # Create a job service object pointing at our host
        js = saga.job.Service(service['scheduler_url'], session)

        # define our job by building a job description and populating it
        jd = saga.job.Description()

        # set the executable to be run
        jd.executable = job_description['executable']

        # set all the information we have been passed

        # set the budget to be charged against
        project = job_description.get('project')
        if project is not None:
            jd.project = project

        num_total_cpus = job_description.get('num_total_cpus')
        if num_total_cpus is not None:
            jd.total_cpu_count = num_total_cpus

        name = job_description.get('name')
        if name is not None:
            jd.name = name

        wallclock_limit = job_description.get('wallclock_limit')
        if wallclock_limit is not None:
            jd.wall_time_limit = wallclock_limit

        # specify where the job's stdout and stderr will go
        jd.output = JOB_STDOUT
        jd.error = JOB_STDERR

        # specify the working directory for the job
        jd.working_directory = REMOTE_WORKING_DIR

        # Some applications may require exclusive use of nodes
        # SAGA does not support this;
        # as a workaround we can pass the following in the spmd_variation property,
        # which is otherwise unused by the underlying SAGA adapter code
        # jd.spmd_variation = "#PBS -l place=excl"

        # Create a new job from the job description. The initial state of
        # the job is 'New'.

        myjob = js.create_job(jd)
        myjob.run()
        js.close()
        return myjob.id

    except saga.SagaException as ex:
        # Catch all saga exceptions
        print('An exception occured: {0} {1}'.format(ex.type, ex))
        # Trace back the exception. That can be helpful for debugging.
        print('Backtrace: {}'.format(ex.traceback))
        return -1



def copy_remote_directory_to_local(remote_dir, local_job_dir):

    if not os.path.exists(local_job_dir):
        os.makedirs(local_job_dir)

    for f in remote_dir.list():
        if remote_dir.is_file(f):
            outfiletarget = 'file://localhost/' + local_job_dir
            remote_dir.copy(f, outfiletarget)
        else:
            path = str(f)
            local_copy_dir = os.path.join(local_job_dir, path)
            copy_remote_directory_to_local(remote_dir.open_dir(f), local_copy_dir)



def stage_output_files(remote_working_dir, local_job_dir, service):
    try:

        if not os.path.exists(local_job_dir):
            os.makedirs(local_job_dir)

        ctx = saga.Context("UserPass")
        ctx.user_id = service['username']
        ctx.user_pass = service['user_pass']

        # create a session and pass our context
        session = saga.Session()
        session.add_context(ctx)

        # create the job's working directory and copy over the contents of our job's output directory

        remote_dir = saga.filesystem.Directory(service['file_url'] + remote_working_dir, session=session)

        for f in remote_dir.list():
            if remote_dir.is_file(f):
                outfiletarget = 'file://localhost/' + local_job_dir
                remote_dir.copy(f, outfiletarget)
            else:
                path = str(f)
                local_copy_dir = os.path.join(local_job_dir, path)
                copy_remote_directory_to_local(remote_dir.open_dir(f), local_copy_dir)

        return 0

    except saga.SagaException as ex:
        # Catch all saga exceptions
        print('An exception occured: {0} {1}'.format(ex.type, ex))
        # Trace back the exception. That can be helpful for debugging.
        print('Backtrace: {}'.format(ex.traceback))
        return -1



def get_saga_job_state(job_id, service):
    ctx = saga.Context("UserPass")
    ctx.user_id = service['username']
    ctx.user_pass = service['user_pass']

    # create a session and pass our context
    session = saga.Session()
    session.add_context(ctx)

    js = saga.job.Service(service['scheduler_url'], session)
    job = js.get_job(job_id)
    state = job.state
    js.close()
    return state





def cleanup_directory(remote_dir, service):
    ctx = saga.Context("UserPass")
    ctx.user_id = service['username']
    ctx.user_pass = service['user_pass']

    # create a session and pass our context
    session = saga.Session()
    session.add_context(ctx)

    # create a session and pass our context
    session = saga.Session()
    session.add_context(ctx)
    remote_dir = saga.filesystem.Directory(service['file_url'] + remote_dir, session=session)
    for f in remote_dir.list():
        remote_dir.remove(f)
    # currently the library does not let us remove remote directories
    #remote_dir.remove()


def main():
    cleanup_directory("/lustre/home/z04/millingw/0635fe93-2f02-4c0a-982d-f4bc35d9926f")



if __name__ == "__main__":
    main()