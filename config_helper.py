import configparser
import os


CONFIG_PATH   = '/data/config/config.ini'
TEMPLATE_PATH = '/opt/iceboxhero/config.ini.template'

# Keys that MUST be explicitly configured — no template default is acceptable.
REQUIRED = {
    'email':   ['smtp_user', 'smtp_pass', 'recipient'],
}


def load_config(config_path=CONFIG_PATH, template_path=TEMPLATE_PATH):
    """Load config.ini, filling missing optional keys from config.ini.template.

    The template is the single source of truth for default values. Any key
    present in the template but missing from config.ini is silently defaulted.
    A missing template is a hard fault — downstream services rely on it for
    optional key defaults and will throw NoOptionError without it.

    Required keys (smtp credentials, sensor ROMs, recipient) must be explicitly
    set in config.ini — template values are rejected.

    All modules must use this helper. Because configparser returns all values
    as strings, callers must use typed getters:
        config.getint('sampling', 'poll_interval')
        config.getfloat('sampling', 'temp_critical')
        config.get('email', 'smtp_user')
    """
    # --- Template is required — hard fault if missing ---
    if not os.path.exists(template_path):
        raise FileNotFoundError(
            f"Configuration template not found at {template_path}. "
            f"Re-run setup.sh to restore it."
        )

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found at {config_path}")

    # --- Parse template ---
    template = configparser.ConfigParser()
    template.optionxform = str
    template.read(template_path)

    # --- Parse config.ini exactly once into a bare parser ---
    # This is the single disk read for config.ini. We use it for:
    #   1. Fallback logging — keys absent here fell back to template defaults
    #   2. Building the final merged config below
    user_config = configparser.ConfigParser()
    user_config.optionxform = str
    user_config.read(config_path)

    # --- Log fallbacks by comparing user_config against template ---
    for section in template.sections():
        if section == 'sensors':
            continue
        for key in template.options(section):
            if not user_config.has_section(section) or \
               not user_config.has_option(section, key):
                print(f"INFO: [{section}] {key} not in config.ini — "
                      f"using template default: {template.get(section, key)}")

    # --- Build final merged config: template defaults + user overrides ---
    # Skip [sensors] — ROM IDs are user-defined and must not be defaulted
    # from the template (which only has placeholder values).
    config = configparser.ConfigParser()
    config.optionxform = str
    for section in template.sections():
        if section == 'sensors':
            continue
        if not config.has_section(section):
            config.add_section(section)
        for key, value in template.items(section):
            config.set(section, key, value)

    # Apply user_config on top of template defaults — no second file read
    for section in user_config.sections():
        if not config.has_section(section):
            config.add_section(section)
        for key, value in user_config.items(section):
            config.set(section, key, value)

    # --- Validate required keys ---
    template_values = {
        section: dict(template.items(section))
        for section in template.sections()
    }

    errors = []

    for section, keys in REQUIRED.items():
        if not config.has_section(section):
            errors.append(f"Missing required section: [{section}]")
            continue
        for key in keys:
            val          = config.get(section, key, fallback='').strip()
            template_val = template_values.get(section, {}).get(key, '').strip()
            # Reject if empty or exactly matches the template placeholder
            if not val or val == template_val:
                errors.append(
                    f"[{section}] {key} is not configured "
                    f"(empty or still set to template placeholder)"
                )

    # Sensors: must have at least one non-placeholder ROM ID
    if not config.has_section('sensors') or not config.items('sensors'):
        errors.append("[sensors] No sensor ROM IDs configured")
    else:
        template_sensor_keys = set(template.options('sensors')) \
            if template.has_section('sensors') else set()
        placeholder_roms = [
            k for k in config.options('sensors')
            if k in template_sensor_keys
        ]
        if placeholder_roms:
            errors.append(
                f"[sensors] Placeholder ROM IDs still present: {placeholder_roms}"
            )
        real_roms = [
            k for k in config.options('sensors')
            if k not in template_sensor_keys
        ]
        if not real_roms:
            errors.append("[sensors] No valid sensor ROM IDs found")

    if errors:
        raise ValueError(
            "Configuration errors in config.ini:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    return config
