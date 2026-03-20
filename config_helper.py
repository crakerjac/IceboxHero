import configparser
import json
import os
import time


def get_sensor_configs(config):
    """Parse all [sensor N] sections and return a list of sensor config dicts.

    Each dict contains:
        id       — DS18B20 ROM ID (e.g. 28-00000071c774)
        name     — friendly display name (e.g. Big Freezer)
        warning  — warning threshold in °F (falls back to [sampling] temp_warning)
        critical — critical threshold in °F (falls back to [sampling] temp_critical)

    Sensors are returned in section order (sensor 1, sensor 2, ...).
    """
    global_warning  = config.getfloat('sampling', 'temp_warning')
    global_critical = config.getfloat('sampling', 'temp_critical')

    sensors = []
    sensor_sections = sorted(
        [s for s in config.sections() if s.lower().startswith('sensor ')],
        key=lambda s: s.lower()
    )
    for section in sensor_sections:
        sensors.append({
            'id':       config.get(section, 'id').strip(),
            'name':     config.get(section, 'name').strip(),
            'warning':  config.getfloat(section, 'warning',  fallback=global_warning),
            'critical': config.getfloat(section, 'critical', fallback=global_critical),
        })
    return sensors


def safe_read_json(path, retries=3):
    """Read and parse a JSON file with retries on decode/IO error.
    Shared utility used by alert_service, display_service, and web_server.
    Returns parsed object or None on failure.
    """
    for _ in range(retries):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05)
    return None


CONFIG_PATH   = '/data/config/config.ini'
TEMPLATE_PATH = '/opt/iceboxhero/config.ini.template'

# Keys that MUST be explicitly configured — no template default is acceptable.
REQUIRED = {
    'email': ['smtp_user', 'smtp_pass', 'recipient'],
}

# Placeholder ROM ID patterns — detected and rejected during validation
ROM_PLACEHOLDERS = ('00000xxxxxxx', '00000yyyyyyy')


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

    # Sensor sections: must have at least one valid [sensor N] section
    sensor_sections = [s for s in config.sections()
                       if s.lower().startswith('sensor ')]
    if not sensor_sections:
        errors.append("No [sensor N] sections found — add at least one sensor configuration")
    else:
        for section in sensor_sections:
            rom_id = config.get(section, 'id', fallback='').strip()
            name   = config.get(section, 'name', fallback='').strip()
            if not rom_id or any(p in rom_id for p in ROM_PLACEHOLDERS):
                errors.append(f"[{section}] id is not configured (placeholder or empty)")
            if not name:
                errors.append(f"[{section}] name is required")

    if errors:
        raise ValueError(
            "Configuration errors in config.ini:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    return config



