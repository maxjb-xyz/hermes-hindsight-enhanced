import os
def get_secret(name, default=None):
    return os.environ.get(name, default)
