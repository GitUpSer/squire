"""Apply a narrowly pinned runtime boundary to Camofox 1.11.2."""
from pathlib import Path
import sys

root=Path(sys.argv[1])
p=root/'server.js'
s=p.read_text()
old='const server = app.listen(PORT, async () => {'
assert s.count(old)==1,'Camofox listener source drift'
s=s.replace(old,'const server = app.listen(PORT, "127.0.0.1", async () => {')
# camoufox-js 0.11.1 made VirtualDisplay.get asynchronous. Camofox 1.11.2
# still assigns the Promise to DISPLAY, which prevents any browser launch.
old='vdDisplay = localVirtualDisplay.get();'
assert s.count(old)==1,'Camofox virtual display source drift'
s=s.replace(old,'vdDisplay = await localVirtualDisplay.get();')
p.write_text(s)
