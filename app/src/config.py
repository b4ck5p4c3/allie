from dynaconf import Dynaconf, Validator

config = Dynaconf(
    envvar_prefix="ALLIE",
    settings_files=['config.yaml'],
    validators=[Validator("nfc.path", must_exist=True)]
)
