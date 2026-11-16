"""pg0 restart regression: unrelated live PID must never be adopted or signalled."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HELPER = Path(__file__).resolve().parents[1]/"bin/squire-pg0-prestart.py"
spec = importlib.util.spec_from_file_location("pg0_prestart", HELPER)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
URL = "pg0://fixture:55432"


@pytest.fixture
def cluster(tmp_path):
    directory = tmp_path/".pg0/instances/fixture"
    data = directory/"data"
    data.mkdir(parents=True)
    (data/"PG_VERSION").write_text("18\n")
    (data/"pg_wal").mkdir()
    (data/"pg_wal/retained-wal").write_bytes(b"unaltered WAL")
    (data/"retained-bank").write_bytes(b"existing restored memory")
    installation = tmp_path/".pg0/installation"
    installation.mkdir()
    metadata = directory/"instance.json"
    record = {"pid":os.getpid(), "port":55432, "data_dir":str(data),
              "installation_dir":str(installation), "username":"hindsight",
              "password":"hindsight", "database":"hindsight", "version":"18.3.0"}
    metadata.write_text(json.dumps(record))
    return tmp_path, data, metadata, record


def repair(cluster, versions=("0.12.3","0.5.2")):
    return guard.repair(cluster[0], URL, *versions)


@pytest.mark.parametrize("versions",[("0.12.3","0.5.2"),("0.15.1","0.9.0")])
def test_reused_live_unrelated_pid_archives_only_metadata(cluster,versions):
    home,data,metadata,record = cluster
    before = {str(path.relative_to(data)):path.read_bytes() for path in data.rglob("*") if path.is_file()}
    with subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]) as unrelated:
        record["pid"] = unrelated.pid
        body = json.dumps(record).encode()
        metadata.write_bytes(body)
        try:
            assert repair(cluster,versions) == "stale_instance_metadata_archived"
            assert unrelated.poll() is None
        finally:
            unrelated.terminate()
            unrelated.wait()
    assert not metadata.exists()
    archives = list(metadata.parent.glob("instance.stale-*.json"))
    assert len(archives) == 1 and archives[0].read_bytes() == body
    assert archives[0].stat().st_mode & 0o777 == 0o600
    assert before == {str(path.relative_to(data)):path.read_bytes() for path in data.rglob("*") if path.is_file()}
    assert repair(cluster,versions) == "no_instance_metadata"
    assert archives[0].read_bytes() == body


def test_exact_live_same_cluster_is_reused_without_metadata_changes(cluster,monkeypatch):
    home,data,metadata,record = cluster
    executable = home/".pg0/installation/18/bin/postgres"
    executable.parent.mkdir(parents=True);executable.touch()
    (data/"postmaster.pid").write_text(f'{record["pid"]}\n{data}\n1\n55432\n/tmp\nlocalhost\n1\nready\n')
    before = metadata.read_bytes()
    monkeypatch.setattr(guard,"process",lambda pid:(executable,[b"postgres",b"-D",os.fsencode(data)]))
    assert repair(cluster) == "existing_postgres_identity_verified"
    assert metadata.read_bytes() == before
    assert (data/"postmaster.pid").exists()
    assert not list(metadata.parent.glob("instance.stale-*"))


@pytest.mark.parametrize("case",["unrelated_pid","wrong_data","wrong_port","missing_process","wrong_executable"])
def test_present_postmaster_requires_positive_cluster_identity(cluster,monkeypatch,case):
    home,data,metadata,record = cluster
    executable=home/".pg0/installation/bin/postgres"
    executable.parent.mkdir();executable.touch()
    lines=[str(record["pid"]),str(data),"1","55432","/tmp","localhost","1","ready"]
    found=(executable,[b"postgres",b"-D",os.fsencode(data)])
    if case=="unrelated_pid": lines[0]="99999"
    if case=="wrong_data": lines[1]=str(home/"other")
    if case=="wrong_port": lines[3]="5432"
    if case=="missing_process": found=None
    if case=="wrong_executable": found=(Path(sys.executable),found[1])
    (data/"postmaster.pid").write_text("\n".join(lines))
    monkeypatch.setattr(guard,"process",lambda pid:found)
    before=metadata.read_bytes()
    with pytest.raises(guard.StartupError): repair(cluster)
    assert metadata.read_bytes()==before and (data/"postmaster.pid").exists()


@pytest.mark.parametrize("case",["data_symlink","metadata_symlink","pidfile_symlink","bad_data","bad_credentials","bad_port","bad_version"])
def test_unsafe_scope_never_archives(cluster,case):
    home,data,metadata,record=cluster
    if case=="data_symlink":
        data.rename(data.with_name("actual"));data.symlink_to(data.with_name("actual"),target_is_directory=True)
    if case=="metadata_symlink":
        metadata.rename(metadata.with_name("actual.json"));metadata.symlink_to(metadata.with_name("actual.json"))
    if case=="pidfile_symlink": (data/"postmaster.pid").symlink_to(home/"missing")
    if case=="bad_data": record["data_dir"]=str(home/"outside")
    if case=="bad_credentials": record["password"]="different-provider-contract"
    if case=="bad_port": record["port"]=5432
    if case=="bad_version": (data/"PG_VERSION").write_text("17")
    if case.startswith("bad_"): metadata.write_text(json.dumps(record))
    with pytest.raises(guard.StartupError): repair(cluster)
    assert metadata.exists() and not list(metadata.parent.glob("instance.stale-*"))


def test_unreadable_process_fails_closed(cluster,monkeypatch):
    def unreadable(pid): raise guard.StartupError("pg0_process_identity_unreadable")
    monkeypatch.setattr(guard,"process",unreadable)
    with pytest.raises(guard.StartupError,match="unreadable"): repair(cluster)
    assert cluster[2].exists()


@pytest.mark.parametrize("exe_disappears",[True,False])
def test_exit_to_zombie_during_identity_read_is_not_a_live_ambiguity(monkeypatch,exe_disappears):
    pid=os.getpid();proc=Path("/proc")/str(pid)
    original_read=Path.read_text;original_resolve=Path.resolve
    states=iter(["S","Z"])
    def read(path,*args,**kwargs):
        if path==proc/"stat":
            return str(pid)+" (fixture) "+" ".join([next(states)]+["0"]*18+["123"])
        return original_read(path,*args,**kwargs)
    def resolve(path,*args,**kwargs):
        if exe_disappears and path==proc/"exe":raise FileNotFoundError()
        return original_resolve(path,*args,**kwargs)
    monkeypatch.setattr(Path,"read_text",read)
    monkeypatch.setattr(Path,"resolve",resolve)
    assert guard.process(pid) is None


def test_missing_executable_of_still_live_process_remains_ambiguous(monkeypatch):
    pid=os.getpid();original_resolve=Path.resolve
    def resolve(path,*args,**kwargs):
        if path==Path("/proc")/str(pid)/"exe":raise FileNotFoundError()
        return original_resolve(path,*args,**kwargs)
    monkeypatch.setattr(Path,"resolve",resolve)
    with pytest.raises(guard.StartupError,match="identity_ambiguous"):guard.process(pid)


def test_concurrent_metadata_replacement_is_preserved(cluster,monkeypatch):
    metadata=cluster[2]
    def replace(data):
        replacement=metadata.with_name("replacement")
        replacement.write_text('{"another":"generation"}')
        replacement.replace(metadata)
    monkeypatch.setattr(guard,"no_live_cluster",replace)
    with pytest.raises(guard.StartupError,match="changed_during_inspection"): repair(cluster)
    assert json.loads(metadata.read_text())=={"another":"generation"}
    assert not list(metadata.parent.glob("instance.stale-*"))


def test_new_postmaster_during_inspection_blocks_archive(cluster,monkeypatch):
    monkeypatch.setattr(guard,"no_live_cluster",lambda data:(data/"postmaster.pid").write_text("new owner"))
    with pytest.raises(guard.StartupError,match="appeared_during_inspection"): repair(cluster)
    assert cluster[2].exists()


def test_unknown_versions_and_traversal_are_rejected(cluster):
    with pytest.raises(guard.StartupError,match="unreviewed"): repair(cluster,("99","0.5.2"))
    with pytest.raises(guard.StartupError,match="unsafe_pg0_instance"):
        guard.repair(cluster[0],"pg0://../other:55432","0.12.3","0.5.2")


def test_exec_lock_excludes_second_api_and_does_not_reach_children(tmp_path):
    # Installed-version lookup is replaced only in this local subprocess; the
    # image build separately exercises both real installed packages/PostgreSQL.
    service = tmp_path/"service.py"
    service.write_text("import os,sys,subprocess\nfrom pathlib import Path\nlock=sys.argv[1]\nassert any(os.readlink(p)==lock for p in Path('/proc/self/fd').iterdir() if p.exists())\nsubprocess.run([sys.executable,'-c',\"import os;from pathlib import Path;assert not any(os.readlink(p)==\"+repr(lock)+\" for p in Path('/proc/self/fd').iterdir() if p.exists())\"],check=True)\nprint('ready',flush=True)\nsys.stdin.read()\n")
    lock=str(tmp_path/".pg0/instances/fixture/.squire-startup.lock")
    code="import importlib.util,sys;spec=importlib.util.spec_from_file_location('g',sys.argv[1]);g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g);g.importlib.metadata.version=lambda n:{'pg0-embedded':'0.12.3','hindsight-api-slim':'0.5.2'}[n];sys.argv=sys.argv[1:];g.main()"
    command=[sys.executable,"-c",code,str(HELPER),sys.executable,str(service),lock]
    env=dict(os.environ,HOME=str(tmp_path),HINDSIGHT_API_DATABASE_URL=URL)
    with subprocess.Popen(command,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as first:
        assert first.stdout.readline().startswith('[squire-pg0-prestart]')
        assert first.stdout.readline().strip()=="ready"
        second=subprocess.run(command,env=env,input="",capture_output=True,text=True,timeout=10)
        assert second.returncode!=0 and "another_hindsight_instance" in second.stderr
        first.communicate("",timeout=10)
        assert first.returncode==0
    third=subprocess.run(command,env=env,input="",capture_output=True,text=True,timeout=10)
    assert third.returncode==0,third.stderr
