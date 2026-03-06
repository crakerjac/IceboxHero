import configparser
import os


def load_config(config_path='/data/config/config.ini'):
    """Utility to load and return the central configuration object.

    All modules must use this helper to read config.ini. Because configparser
    returns all values as strings, callers must use typed getters:
        config.getint('sampling', 'poll_interval')
        config.getfloat('sampling', 'temp_critical')
        config.get('email', 'smtp_user')
    """
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found at {config_path}")

    config = configparser.ConfigParser()
    config.optionxform = str  # Preserve key case (needed for sensor ROM IDs)
    config.read(config_path)
    return config
