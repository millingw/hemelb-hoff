# Create dummy secret key so we can use sessions
SECRET_KEY = '*********'

# Name of the MariaDB database we will use
DATABASE_FILE = 'compbiomed'

# the complete path to our running database
# note that the account used here should not have any schema modification permissions,
# ie no table drops or creation
# this should have the form 'mysql://username:password@localhost/' + DATABASE_FILE
# assuming webservice and database are on same server, config database to only accept requests on localhost
SQLALCHEMY_DATABASE_URI = 'mysql://<user>:<pass>@localhost/' + DATABASE_FILE

# path for static web content
APP_STATIC_URL = '/home/ubuntu/PycharmProjects/hemelb-hoff/static'

# Application logging file name
APP_LOGFILE = '/home/ubuntu/foo.log'

SQLALCHEMY_ECHO = False

# Flask-Security config
SECURITY_URL_PREFIX = "/admin"
# specify the hashing algorithm
SECURITY_PASSWORD_HASH = "pbkdf2_sha512"
# specify the password salt. should be a suitable long random string.
SECURITY_PASSWORD_SALT = "*****************************"

# Flask-Security URLs, overridden because they don't put a / at the end
SECURITY_LOGIN_URL = "/login/"
SECURITY_LOGOUT_URL = "/logout/"
SECURITY_REGISTER_URL = "/register/"

SECURITY_POST_LOGIN_VIEW = "/admin/"
SECURITY_POST_LOGOUT_VIEW = "/admin/"
SECURITY_POST_REGISTER_VIEW = "/admin/"

# Flask-Security features
SECURITY_REGISTERABLE = True
SECURITY_SEND_REGISTER_EMAIL = False
SQLALCHEMY_TRACK_MODIFICATIONS = False
SECURITY_RECOVERABLE = True
SECURITY_CHANGEABLE = True

# this must be false or the unit tests can't log in correctly
# set to true for production deployment
WTF_CSRF_ENABLED = False

# Location for holding input file before staging
INPUT_STAGING_AREA = '/home/ubuntu/jobs/input'

# Location for holding output files
OUTPUT_STAGING_AREA = '/home/ubuntu/jobs/output'

# local for holding input filesets
INPUTSET_STAGING_AREA = '/home/ubuntu/inputsets'


# Location for temnporary files
TEMP_FOLDER = "/home/ubuntu/temp"

# maximum number of jobs a user may have.
# DELETED jobs are not counted
MAX_USER_JOBS = 10

# time (in minutes) to wait before checking remote job state
REMOTE_JOB_STATE_REFRESH_PERIOD = 2

# WOS config stuff
USE_WOS = True
# configer the S3 endpoint, region and credentials. Could be any S3-compatible service, but here we assume its the CIRRUS WOS
CIRRUS_S3_ENDPOINT = "********"
CIRRUS_WOS_ACCESS_KEY = ""
CIRRUS_WOS_SECRET_KEY = ""
CIRRUS_WOS_HOFF_BUCKET = ""
CIRRUS_S3_DEFAULT_REGION = ""
