"""Apply a narrowly pinned runtime boundary to Camofox 1.11.2."""
from pathlib import Path
import sys

root=Path(sys.argv[1])
p=root/'server.js'
s=p.read_text()
old='const server = app.listen(PORT, async () => {'
assert s.count(old)==1,'Camofox listener source drift'
s=s.replace(old,'const server = app.listen(PORT, "127.0.0.1", async () => {')
p.write_text(s)
