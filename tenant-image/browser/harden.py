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

# Firefox 135 beta.24's shipped Juggler Protocol.js accepts only viewportSize
# (+ deviceScaleFactor on the context method). Playwright 1.61 sends newer
# isMobile/screenSize fields. Preserve supported desktop viewport behavior,
# and reject explicit unsupported emulation instead of silently discarding it.
p=root/'node_modules/playwright-core/lib/coreBundle.js'
s=p.read_text()
old='''      async updateEmulatedViewportSize() {
        const emulatedSize = this._page.emulatedSize();
        await this._session.send("Page.setViewportSize", {
          viewportSize: emulatedSize?.viewport ?? null,
          screenSize: emulatedSize?.screen,
          isMobile: !!this._browserContext._options.isMobile
        });
      }'''
new='''      async updateEmulatedViewportSize() {
        if (this._browserContext._options.isMobile || this._browserContext._options.screen)
          throw new Error("Pinned Camoufox does not support mobile or separate screen emulation");
        const emulatedSize = this._page.emulatedSize();
        await this._session.send("Page.setViewportSize", {
          viewportSize: emulatedSize?.viewport ?? null
        });
      }'''
assert s.count(old)==1,'Playwright page viewport source drift'
s=s.replace(old,new)
old='''      async doUpdateDefaultViewport() {
        if (!this._options.viewport)
          return;
        const viewport = {
          viewportSize: { width: this._options.viewport.width, height: this._options.viewport.height },
          screenSize: this._options.screen,
          deviceScaleFactor: this._options.deviceScaleFactor || 1,
          isMobile: !!this._options.isMobile
        };
        await this._browser.session.send("Browser.setDefaultViewport", { browserContextId: this._browserContextId, viewport });
      }'''
new='''      async doUpdateDefaultViewport() {
        if (this._options.isMobile || this._options.screen)
          throw new Error("Pinned Camoufox does not support mobile or separate screen emulation");
        if (!this._options.viewport)
          return;
        const viewport = {
          viewportSize: { width: this._options.viewport.width, height: this._options.viewport.height },
          deviceScaleFactor: this._options.deviceScaleFactor || 1
        };
        await this._browser.session.send("Browser.setDefaultViewport", { browserContextId: this._browserContextId, viewport });
      }'''
assert s.count(old)==1,'Playwright context viewport source drift'
s=s.replace(old,new)
p.write_text(s)
