"""휴대폰 카메라 QR 스캐너 (Streamlit custom component v2, 인라인).

버튼을 누르면 후면 카메라를 켜고 화면에서 QR을 계속 찾는다. 읽히면 카메라를 끄고 값을 Python으로 보낸다
(trigger 'scanned'). 브라우저 내장 BarcodeDetector가 있으면(안드로이드 크롬) 그걸 쓰고, 없으면(아이폰 사파리 등)
jsQR을 CDN에서 불러 쓴다. 카메라는 HTTPS(또는 localhost)에서만 켜진다."""

import streamlit as st

_HTML = """
<div class="qr">
  <button type="button" class="start">QR 스캔 시작</button>
  <div class="msg"></div>
  <video playsinline muted hidden></video>
</div>
"""

_CSS = """
.qr { font-family: var(--st-font); color: var(--st-text-color); }
.qr .start {
  width: 100%; padding: 0.9rem; font-size: 1.05rem; font-weight: 600; cursor: pointer;
  border: 1px solid var(--st-primary-color); border-radius: var(--st-base-radius, 0.5rem);
  background: var(--st-primary-color); color: #fff;
}
.qr .start.on { background: transparent; color: var(--st-primary-color); }
.qr .msg { font-size: 0.85rem; opacity: 0.75; margin: 0.4rem 0; min-height: 1.1rem; }
.qr video { width: 100%; max-height: 60vh; border-radius: var(--st-base-radius, 0.5rem); background: #000; object-fit: cover; }
"""

_JS = """
const instances = new WeakMap()

export default function (component) {
  const { parentElement, setTriggerValue } = component
  if (instances.has(parentElement)) return
  const root = parentElement.querySelector(".qr")
  const btn = root.querySelector(".start")
  const video = root.querySelector("video")
  const msg = root.querySelector(".msg")
  const canvas = document.createElement("canvas")
  const ctx2d = canvas.getContext("2d", { willReadFrequently: true })
  const st = { stream: null, timer: null, detector: null, jsQR: null }
  instances.set(parentElement, st)

  const stop = (text) => {
    if (st.timer) clearTimeout(st.timer)
    st.timer = null
    if (st.stream) st.stream.getTracks().forEach((t) => t.stop())
    st.stream = null
    video.srcObject = null
    video.hidden = true
    btn.textContent = "QR 스캔 시작"
    btn.classList.remove("on")
    msg.textContent = text || ""
  }

  async function loadDecoder() {
    if (st.detector || st.jsQR) return
    if ("BarcodeDetector" in window) {
      try {
        const formats = await window.BarcodeDetector.getSupportedFormats()
        if (formats.includes("qr_code")) {
          st.detector = new window.BarcodeDetector({ formats: formats.filter((f) => ["qr_code", "code_128", "code_39", "ean_13"].includes(f)) })
          return
        }
      } catch (e) {}
    }
    const mod = await import("https://cdn.jsdelivr.net/npm/jsqr@1.4.0/+esm")
    st.jsQR = mod.default
  }

  async function decodeFrame() {
    if (st.detector) {
      const codes = await st.detector.detect(video)
      return codes.length ? codes[0].rawValue : null
    }
    const scale = Math.min(1, 720 / video.videoWidth)
    canvas.width = Math.round(video.videoWidth * scale)
    canvas.height = Math.round(video.videoHeight * scale)
    ctx2d.drawImage(video, 0, 0, canvas.width, canvas.height)
    const img = ctx2d.getImageData(0, 0, canvas.width, canvas.height)
    const code = st.jsQR(img.data, img.width, img.height, { inversionAttempts: "attemptBoth" })
    return code ? code.data : null
  }

  async function loop() {
    // 탭을 옮기는 등 화면에서 사라지면 카메라를 끈다.
    if (!st.stream) return
    if (!root.isConnected) return stop()
    try {
      if (video.readyState >= 2 && video.videoWidth) {
        const value = await decodeFrame()
        if (value && value.trim()) {
          if (navigator.vibrate) navigator.vibrate(120)
          stop("읽음: " + value.trim())
          setTriggerValue("scanned", value.trim())
          return
        }
      }
    } catch (e) {}
    st.timer = setTimeout(loop, 150)
  }

  btn.onclick = async () => {
    if (st.stream) return stop()
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      msg.textContent = "이 브라우저에서는 카메라를 쓸 수 없어요. 시리얼을 직접 입력해 주세요."
      return
    }
    msg.textContent = "카메라 켜는 중…"
    try {
      await loadDecoder()
      st.stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      })
    } catch (e) {
      stop("카메라를 켜지 못했어요 (" + (e && e.name ? e.name : e) + "). 브라우저에서 카메라 권한을 허용해 주세요.")
      return
    }
    video.srcObject = st.stream
    video.hidden = false
    await video.play().catch(() => {})
    btn.textContent = "스캔 멈추기"
    btn.classList.add("on")
    msg.textContent = "QR을 화면 가운데에 비춰 주세요."
    loop()
  }
}
"""

_SCANNER = st.components.v2.component("qr_scanner", html=_HTML, css=_CSS, js=_JS)


def qr_scanner(key, on_scan):
    """스캔 버튼 + 카메라. 읽히면 on_scan()이 불리고, 그 안에서 st.session_state[key].scanned로 값을 읽는다."""
    return _SCANNER(key=key, on_scanned_change=on_scan)
