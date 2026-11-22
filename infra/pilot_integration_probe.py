#!/usr/bin/env python3
"""Bounded read-only integration inventory; emits paths/key names, never values."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


def probe(home=Path.home()):
    roots = [".camofox", ".config", ".codex", ".claude", ".ssh"]
    result = {"external_roots": {}, "config_paths": [], "mcp": [], "browser": {}}
    for relative in roots:
        root = home / relative
        size, count, links = 0, 0, []
        if root.is_dir():
            for current, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [name for name in dirs if name not in {"node_modules", ".git", "cache", "Cache", "Code Cache", "GPUCache", "logs"}]
                for name in files:
                    item = Path(current) / name
                    if item.is_symlink():
                        links.append(str(item.relative_to(home)))
                    elif item.is_file():
                        count += 1
                        size += item.stat().st_size
                    if count >= 100000:
                        break
        result["external_roots"][relative] = {"exists": root.exists(), "files": count, "bytes": size, "symlinks": links[:30]}
    for relative in (".hermes/config.yaml", ".hermes/config.yml"):
        path = home / relative
        if not path.is_file():
            continue
        result["config_paths"].append(relative)
        import yaml
        config = yaml.safe_load(path.read_text()) or {}
        if not isinstance(config, dict):
            continue
        result["top_level_keys"] = sorted(config)
        for name, value in config.items():
            if "browser" in name.lower() and isinstance(value, dict):
                result["browser"][name] = {"keys": sorted(value)}
                for key in ("backend", "provider", "engine"):
                    if isinstance(value.get(key), str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value[key]):
                        result["browser"][name][key] = value[key]
                camofox = value.get("camofox")
                if isinstance(camofox, dict):
                    result["browser"][name]["camofox_keys"] = sorted(camofox)
                    for key in ("url", "base_url", "endpoint"):
                        if isinstance(camofox.get(key), str):
                            parsed = urlsplit(camofox[key])
                            result["browser"][name]["camofox_host"] = parsed.hostname
                            result["browser"][name]["camofox_port"] = parsed.port
            if "mcp" in name.lower() and isinstance(value, dict):
                servers = value.get("servers", value)
                for server, options in servers.items():
                    if not isinstance(options, dict):
                        continue
                    entry = {"name": str(server), "keys": sorted(options), "env_keys": sorted(options.get("env", {}))}
                    command = options.get("command")
                    if isinstance(command, str) and re.fullmatch(r"[A-Za-z0-9_./~+-]{1,300}", command):
                        entry["command"] = command
                    url = options.get("url")
                    if isinstance(url, str):
                        parsed = urlsplit(url)
                        entry["endpoint_host"] = parsed.hostname
                        entry["endpoint_port"] = parsed.port
                    result["mcp"].append(entry)
        # Only rooted path tokens, never arbitrary command arguments or values.
        result["referenced_home_paths"] = sorted(set(re.findall(r"/home/hermes/[A-Za-z0-9_./+-]+", path.read_text())))
    script_link = home / ".hermes/scripts/data"
    result["scripts_data_link"] = {"is_symlink": script_link.is_symlink(), "target_exists": script_link.exists()}
    if script_link.is_symlink():
        result["scripts_data_link"]["resolved_path"] = str(script_link.resolve())
    return result


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))
