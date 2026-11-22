"""Decode a public upstream speech fixture as WhatsApp Ogg/Opus, entirely offline."""
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import resource
import socket
import subprocess
import sys
import tempfile
import time


def main():
    model=Path(sys.argv[1] if len(sys.argv)>1 else "/opt/squire/stt/base")
    fixture=Path(sys.argv[2] if len(sys.argv)>2 else "/opt/hermes/tools/neutts_samples/jo.wav")
    assert hashlib.sha256(fixture.read_bytes()).hexdigest()=="f789bc3799e64675a6324a978a3fffb0dd7850739d232cbe3d4bbbbc9973a314"
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in model.iterdir() if p.is_file()}
    assert before["model.bin"]=="d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"
    assert not os.access(model,os.W_OK),"model cache must be read-only to the runtime user"
    with tempfile.TemporaryDirectory(prefix="squire-stt-smoke-") as directory:
        os.environ["HERMES_HOME"]=directory
        os.environ["HERMES_DISABLE_LAZY_INSTALLS"]="1"
        config={"stt":{"enabled":True,"provider":"local","local":{"model":str(model),"device":"cpu","compute_type":"int8"}},"security":{"allow_lazy_installs":False}}
        # JSON is also YAML; use the actual Hermes config loader and STT entrypoint.
        Path(directory,"config.yaml").write_text(json.dumps(config))
        audio=Path(directory,"voice.ogg")
        subprocess.run(["ffmpeg","-nostdin","-v","error","-i",str(fixture),"-c:a","libopus",str(audio)],check=True)
        from tools.transcription_tools import transcribe_audio
        def no_network(*args,**kwargs):raise AssertionError("offline transcription attempted network access")
        socket.socket.connect=no_network
        socket.socket.connect_ex=no_network
        started=time.monotonic()
        result=transcribe_audio(str(audio),source="gateway")
        assert result.get("success") and result.get("provider")=="local","local transcription failed"
        text=result["transcript"].lower()
        assert all(word in text for word in ("responsive","voice","conversations")),"speech content was not recovered"
        after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in model.iterdir() if p.is_file()}
        assert before==after,"read-only model changed"
        peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
        assert peak<1536*1024*1024,"STT exceeds its memory allowance alongside Hindsight"
        cgroup=Path("/sys/fs/cgroup")
        limits={name:(cgroup/name).read_text().strip() for name in ("memory.current","memory.peak","memory.max","cpu.max") if (cgroup/name).exists()}
        print(json.dumps({"local_transcription":True,"ogg_opus_decoded":True,"network_allowed":False,"cache_unchanged":True,"speech_keywords_matched":3,"uid":os.getuid(),"versions":{name:version(name) for name in ("faster-whisper","ctranslate2","av")},"elapsed_seconds":round(time.monotonic()-started,2),"process_peak_bytes":peak,"cgroup":limits}))


if __name__=="__main__":main()
