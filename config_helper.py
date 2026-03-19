import configparser
import os


CONFIG_PATH   = '/data/config/config.ini'
TEMPLATE_PATH = '/opt/iceboxhero/config.ini.template'

# Keys that MUST be explicitly configured — no template default is acceptable.
# Validated by checking for placeholder values or empty strings.
REQUIRED = {
    'email':   ['smtp_user', 'smtp_pass', 'recipient'],
    'sensors': [],  # Checked separately — must have at least one real ROM ID
}

# Placeholder patterns that indicate a key has not been configured
PLACEHOLDERS = ('your_', 'xxxx', '00000xxxxxxx', '00000yyyyyyy')


def load_config(config_path=CONFIG_PATH, template_path=TEMPLATE_PATH):
    """Load config.ini, filling missing optional keys from config.ini.template.

    The template is the single source of truth for default values. Any key
    present in the template but missing from config.ini is silently defaulted.
    Required keys (smtp credentials, sensor ROMs, recipient) must be explicitly
    set in config.ini — template placeholder values are rejected.

    All modules must use this helper. Because configparser returns all values
    as strings, callers must use typed getters:
        config.getint('sampling', 'poll_interval')
        config.getfloat('sampling', 'temp_critical')
        config.get('email', 'smtp_user')
    """
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found at {config_path}")

    # --- Load template as defaults ---
    template = configparser.ConfigParser()
    template.optionxform = str
    if os.path.exists(template_path):
        template.read(template_path)
    else:
        print(f"WARNING: Template not found at {template_path} — no defaults applied.")

    # --- Build config seeded with template defaults ---
    config = configparser.ConfigParser()
    config.optionxform = str

    # Copy all template sections/keys as defaults.
    # Skip [sensors] — ROM IDs are user-defined and must not be defaulted
    # from the template (which only has placeholder values).
    for section in template.sections():
        if section == "sensors":
            continue
        if not config.has_section(section):
            config.add_section(section)
        for key, value in template.items(section):
            config.set(section, key, value)

    # Override with actual config.ini values
    config.read(config_path)

    # --- Log any keys that fell back to template defaults ---
    actual = configparser.ConfigParser()
    actual.optionxform = str
    actual.read(config_path)

    for section in template.sections():
        if section == 'sensors':
            continue  # Sensor ROMs are user-defined, not in template defaults
        for key in template.options(section):
            if not actual.has_section(section) or not actual.has_option(section, key):
                print(f"INFO: [{section}] {key} not in config.ini — using template default: "
                      f"{template.get(section, key)}")

    # --- Validate required keys ---
    errors = []

    for section, keys in REQUIRED.items():
        if not config.has_section(section):
            errors.append(f"Missing required section: [{section}]")
            continue
        for key in keys:
            val = config.get(section, key, fallback='').strip()
            if not val or any(val.startswith(p) for p in PLACEHOLDERS):
                errors.append(f"[{section}] {key} is not configured (placeholder or empty)")

    # Sensors: must have at least one non-placeholder ROM ID
    if not config.has_section('sensors') or not config.items('sensors'):
        errors.append("[sensors] No sensor ROM IDs configured")
    else:
        placeholder_roms = [
            k for k, v in config.items('sensors')
            if any(p in k for p in PLACEHOLDERS)
        ]
        if placeholder_roms:
            errors.append(f"[sensors] Placeholder ROM IDs still present: {placeholder_roms}")
        real_roms = [k for k in config.options('sensors')
                     if not any(p in k for p in PLACEHOLDERS)]
        if not real_roms:
            errors.append("[sensors] No valid sensor ROM IDs found")

    if errors:
        raise ValueError(
            "Configuration errors in config.ini:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    return config
