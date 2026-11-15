"""Private browser cookie/localStorage persistence: tmpfs live, AEAD on volume."""
import io
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import zipfile

MAX_STATE=50*1024*1024


def crypto():
    # Existing envelope format and DEK; no second tenant credential authority.
    import importlib.util
    spec=importlib.util.spec_from_file_location('squire_browser_crypto',os.environ.get('SQUIRE_CRYPTO_MODULE','/opt/squire/bin/squire_secrets.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class BrowserState:
    def __init__(self,home,runtime_id,volatile_root='/dev/shm'):
        self.home=Path(home);self.runtime_id=runtime_id
        self.root=Path(tempfile.mkdtemp(prefix='squire-browser-',dir=volatile_root));self.root.chmod(0o700)
        self.encrypted=self.home/'.squire'/'browser-state.enc'
        self.encrypted.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.codec=crypto();self.key=self.codec.load_dek()
        self.aad='browser-state:v1:'+runtime_id

    def restore(self):
        if not self.encrypted.exists():return
        raw=self.codec.unseal_bytes(self.key,self.encrypted.read_bytes(),self.aad)
        if len(raw)>MAX_STATE:raise ValueError('browser state exceeds limit')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            total=0
            for entry in archive.infolist():
                path=Path(entry.filename)
                if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in {'profiles','cookies','checkpoints','private'}:
                    raise ValueError('invalid browser state path')
                total+=entry.file_size
                if total>MAX_STATE:raise ValueError('browser state exceeds limit')
                if entry.is_dir():continue
                dest=self.root/path;dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                dest.write_bytes(archive.read(entry));dest.chmod(0o600)

    def import_from(self, source):
        """Read-only migration input; never delete or modify the founder source."""
        import shutil
        source=Path(source)
        for name in ('profiles','cookies','checkpoints','private'):
            directory=source/name
            if not directory.exists():continue
            if directory.is_symlink() or any(p.is_symlink() for p in directory.rglob('*')):
                raise ValueError('browser import symlink refused')
            shutil.copytree(directory,self.root/name,dirs_exist_ok=True)
        self.save()

    def save(self):
        data=io.BytesIO();total=0
        with zipfile.ZipFile(data,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for dirname in ('profiles','cookies','checkpoints','private'):
                directory=self.root/dirname
                if not directory.exists():continue
                for p in directory.rglob('*'):
                    if p.is_symlink():raise ValueError('browser state symlink refused')
                    if not p.is_file():continue
                    size=p.stat().st_size;total+=size
                    if total>MAX_STATE:raise ValueError('browser state exceeds limit')
                    archive.writestr(p.relative_to(self.root).as_posix(),p.read_bytes())
        if data.tell()>MAX_STATE:raise ValueError('browser archive exceeds limit')
        ciphertext=self.codec.seal_bytes(self.key,data.getvalue(),self.aad)
        fd,name=tempfile.mkstemp(prefix='.browser-state-',dir=self.encrypted.parent)
        try:
            with os.fdopen(fd,'wb') as handle:handle.write(ciphertext);handle.flush();os.fsync(handle.fileno())
            os.replace(name,self.encrypted)
            dirfd=os.open(self.encrypted.parent,os.O_DIRECTORY)
            try:os.fsync(dirfd)
            finally:os.close(dirfd)
        finally:
            if os.path.exists(name):os.unlink(name)


def browser_environment():
    """The browser subprocess never needs model keys or messaging/control grants."""
    allowed={'PATH','HOME','LANG','LC_ALL','TZ','LD_LIBRARY_PATH','SSL_CERT_FILE','SSL_CERT_DIR','XDG_CACHE_HOME'}
    return {key:value for key,value in os.environ.items()
            if key in allowed or key.startswith(('CAMOFOX_','CAMOUFOX_','PROXY_'))}


def run():
    # Group services and legacy Telegram never start a browser or read browser secrets.
    if os.environ.get('SQUIRE_RUNTIME_KIND')!='dm':return
    from .config import RuntimeConfig
    scope=RuntimeConfig.from_env()
    state=BrowserState(scope.home,scope.runtime_id);state.restore()
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    env=dict(browser_environment(),CAMOFOX_PORT='9377',ENABLE_VNC='0',NODE_ENV='development',
        CAMOFOX_PROFILE_DIR=str(state.root/'profiles'),CAMOFOX_COOKIES_DIR=str(state.root/'cookies'),
        CAMOFOX_TRACES_DIR=str(state.root/'traces'),CAMOFOX_CRASH_REPORT_ENABLED='false',SENTRY_DSN='',
        MAX_SESSIONS='3',MAX_TABS_GLOBAL='6',MAX_TABS_PER_SESSION='3',MAX_CONCURRENT_PER_USER='1',
        BROWSER_IDLE_TIMEOUT_MS='7200000',TAB_INACTIVITY_MS='7200000',SESSION_TIMEOUT_MS='7200000',
        BROWSER_RSS_RESTART_THRESHOLD_MB='1200')
    # Loopback-only API belongs to this private runtime. Preserve imported API keys
    # if present; no public listener, no external tunnel or unauthenticated viewer.
    proc=subprocess.Popen(['node','--max-old-space-size=256','/opt/squire/camofox/server.js'],env=env,
        cwd='/opt/squire/camofox',start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        while proc.poll() is None and not stop.wait(5):state.save()
        if not stop.is_set():raise RuntimeError('private browser service exited')
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(10)
            except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        state.save()
        # Live plaintext is only on tmpfs; remove it after sealing the final state.
        import shutil
        shutil.rmtree(state.root)

if __name__=='__main__':run()
