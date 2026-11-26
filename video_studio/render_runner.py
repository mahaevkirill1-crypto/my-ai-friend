"""Изолированный процесс рендера одной сцены. Читает JSON из stdin. НЕ импортирует остальной код проекта.
Требует playwright+chromium и ffmpeg (см. deploy/Dockerfile.video-worker). В тестовой среде не запускался."""
import json, subprocess, sys

CSP = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; "
       "style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:\">")


def main():
    spec = json.loads(sys.stdin.read())
    w, h, fps, dur = int(spec["w"]), int(spec["h"]), int(spec["fps"]), float(spec["duration"])
    frames = int(round(dur * fps))
    if frames < 1 or frames > 20 * fps * 12: raise SystemExit("bad frame count")
    from playwright.sync_api import sync_playwright
    html = spec["html"]
    html = CSP + html if "<head" not in html.lower() else html.replace("<head>", "<head>" + CSP, 1)
    ff = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(fps), "-i", "-", "-c:v", "libx264",
                           "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart", spec["out"]],
                          stdin=subprocess.PIPE)
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--disable-extensions", "--host-resolver-rules=MAP * ~NOTFOUND", "--disable-dev-shm-usage"])
        ctx = br.new_context(offline=True, viewport={"width": w, "height": h}, service_workers="block")
        ctx.route("**/*", lambda route: route.abort())          # любой сетевой запрос блокируется
        page = ctx.new_page()
        page.set_content(html, wait_until="load")
        if not page.evaluate("typeof window.__setTime === 'function'"): raise SystemExit("no __setTime")
        for i in range(frames):
            page.evaluate("t => window.__setTime(t)", i / fps)
            ff.stdin.write(page.screenshot(type="png"))
        br.close()
    ff.stdin.close()
    if ff.wait() != 0: raise SystemExit("ffmpeg failed")


if __name__ == "__main__":
    main()
