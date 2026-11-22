import yaml
from squire_overlay.bootstrap import prepare


def test_pilot_uses_baked_local_base_without_changing_other_audio_settings(scope):
    path=scope.home/"config.yaml"
    path.write_text(yaml.safe_dump({"stt":{"provider":"local","enabled":True,"local":{"model":"base","language":"en"}},"tts":{"provider":"existing"}}))
    prepare()
    config=yaml.safe_load(path.read_text())
    assert config["stt"]=={"enabled":True,"provider":"local","local":{"model":"/opt/squire/stt/base","device":"cpu","compute_type":"int8","language":"en"}}
    assert config["tts"]=={"provider":"existing"}
