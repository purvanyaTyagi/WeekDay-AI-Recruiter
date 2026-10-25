// Candidate interview chat: SSE for streamed agent replies, fetch() for sending.
(function () {
  const { token } = window.INTERVIEW;
  const base = "/i/" + token;
  const log = document.getElementById("log");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("send-btn");
  const retryBtn = document.getElementById("retry-btn");
  const typing = document.getElementById("typing");
  const notice = document.getElementById("notice");
  let partialEl = null, partialText = "", generating = false, closed = false, source = null;

  function bubble(role, text) {
    const wrap = document.createElement("div");
    wrap.className = role === "candidate" ? "flex justify-end" : "flex justify-start";
    const b = document.createElement("div");
    b.className = "max-w-[85%] rounded-lg px-3 py-2 text-sm prose-pre " +
      (role === "candidate" ? "bg-indigo-600 text-white" : "bg-white border border-slate-200");
    b.textContent = text;  // never innerHTML: content is untrusted
    wrap.appendChild(b);
    log.appendChild(wrap);
    window.scrollTo(0, document.body.scrollHeight);
    return b;
  }

  function setNotice(text, kind) {
    if (!text) { notice.classList.add("hidden"); return; }
    notice.textContent = text;
    notice.className = "mb-3 rounded border px-3 py-2 text-sm " +
      (kind === "error" ? "bg-red-50 border-red-200 text-red-800" : "bg-green-50 border-green-200 text-green-800");
  }

  function setGenerating(on) {
    generating = on;
    typing.textContent = on ? "Interviewer is typing…" : "";
    sendBtn.disabled = on || closed;
  }

  function close(message) {
    closed = true;
    if (recorder && recorder.state === "recording") recorder.stop();
    if (micBtn) micBtn.disabled = true;
    input.disabled = true; sendBtn.disabled = true; retryBtn.classList.add("hidden");
    setGenerating(false);
    setNotice(message || "This interview has ended.", "ok");
    if (source) source.close();
  }

  function startPartial() { partialText = ""; partialEl = bubble("agent", ""); setGenerating(true); }

  function connect() {
    if (new URLSearchParams(location.search).get("transport") === "poll") { startPolling(); return; }
    let gotSnapshot = false;
    // Some proxies and tunnels (e.g. Cloudflare quick tunnels) buffer SSE, so nothing ever arrives.
    setTimeout(() => { if (!gotSnapshot) startPolling(); }, 5000);
    source = new EventSource(base + "/events");
    source.addEventListener("snapshot", (e) => {
      gotSnapshot = true;
      const s = JSON.parse(e.data);
      log.innerHTML = ""; partialEl = null; partialText = "";
      s.entries.forEach((en) => bubble(en.role, en.content));
      if (s.closed_message) { close(s.closed_message); return; }
      if (s.generating) { startPartial(); partialText = s.partial; partialEl.textContent = s.partial; }
      else setGenerating(false);
      if (s.needs_turn) retry();
    });
    source.addEventListener("agent_start", () => { setNotice(""); retryBtn.classList.add("hidden"); startPartial(); });
    source.addEventListener("chunk", (e) => {
      const { offset, text } = JSON.parse(e.data);
      if (!partialEl) startPartial();
      // Offsets let us ignore chunks already included in the snapshot after a reconnect.
      if (offset + text.length <= partialText.length) return;
      partialText += text.slice(Math.max(0, partialText.length - offset));
      partialEl.textContent = partialText;
      window.scrollTo(0, document.body.scrollHeight);
    });
    source.addEventListener("agent_done", (e) => {
      const { entry } = JSON.parse(e.data);
      if (partialEl) partialEl.textContent = entry.content; else bubble("agent", entry.content);
      partialEl = null; partialText = "";
      setGenerating(false); input.focus();
      speakEntry(entry);
    });
    source.addEventListener("agent_error", (e) => {
      if (partialEl) partialEl.parentElement.remove();
      partialEl = null; partialText = ""; setGenerating(false);
      setNotice(JSON.parse(e.data).message, "error");
      retryBtn.classList.remove("hidden");
    });
    source.addEventListener("candidate", (e) => {
      // Echo from another tab; this tab already showed its own message.
      const { entry } = JSON.parse(e.data);
      if (!log.dataset.lastSent || log.dataset.lastSent !== entry.content) bubble("candidate", entry.content);
      log.dataset.lastSent = "";
    });
    source.addEventListener("status", (e) => close(JSON.parse(e.data).message));
    source.onerror = () => { if (!closed) typing.textContent = "Reconnecting…"; };
  }

  // Polling fallback: the same snapshot data as JSON, fetched every 1–2 s.
  let polling = false, lastSeq = 0, firstPoll = true, optimisticEl = null;

  function startPolling() {
    if (polling || closed) return;
    polling = true;
    if (source) { source.close(); source = null; }
    poll();
  }

  async function poll() {
    if (closed) return;
    let s = null;
    try {
      const r = await fetch(base + "/poll?after=" + lastSeq, { cache: "no-store" });
      if (r.ok) s = await r.json();
    } catch (_) {}
    if (!s) { typing.textContent = "Reconnecting…"; setTimeout(poll, 3000); return; }
    applyPoll(s);
    if (!closed) setTimeout(poll, generating ? 1000 : 2000);
  }

  function applyPoll(s) {
    if (firstPoll) { log.innerHTML = ""; partialEl = null; partialText = ""; }
    s.entries.forEach((en) => {
      lastSeq = Math.max(lastSeq, en.seq);
      // Reuse the bubbles already on screen: the candidate's own message, or the agent's streamed text.
      if (en.role === "candidate" && optimisticEl) { optimisticEl.textContent = en.content; optimisticEl = null; }
      else if (en.role === "agent" && partialEl) { partialEl.textContent = en.content; partialEl = null; partialText = ""; }
      else bubble(en.role, en.content);
      if (en.role === "agent" && !firstPoll) speakEntry(en);  // don't read out history on load
    });
    if (s.closed_message) { close(s.closed_message); return; }
    if (s.generating) {
      setNotice(""); retryBtn.classList.add("hidden");
      if (s.partial) {
        if (!partialEl) startPartial();
        partialText = s.partial; partialEl.textContent = s.partial;
        window.scrollTo(0, document.body.scrollHeight);
      }
      setGenerating(true);
    } else {
      if (partialEl) { partialEl.parentElement.remove(); partialEl = null; partialText = ""; }
      setGenerating(false);
      if (s.needs_turn && firstPoll) retry();
      else if (s.needs_turn) {
        // The candidate spoke last and nothing is running: the agent's turn failed.
        setNotice("The interviewer had a technical problem. Click Retry to continue.", "error");
        retryBtn.classList.remove("hidden");
      }
    }
    firstPoll = false;
  }

  async function post(path, body) {
    const r = await fetch(base + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let data = {}; try { data = await r.json(); } catch (_) {}
    return { ok: r.ok, status: r.status, data };
  }

  async function send() {
    const text = input.value.trim();
    if (!text || generating || closed) return;
    sendBtn.disabled = true;
    log.dataset.lastSent = text;
    // When polling, show the message first so a poll that returns it can reuse the bubble.
    if (polling) optimisticEl = bubble("candidate", text);
    const r = await post("/message", { text });
    if (r.ok) { if (!polling) bubble("candidate", text); input.value = ""; setGenerating(true); }
    else {
      log.dataset.lastSent = "";
      if (optimisticEl) { optimisticEl.parentElement.remove(); optimisticEl = null; }
      if (r.status === 410) close(r.data.error); else { setNotice(r.data.error || "Could not send. Try again.", "error"); sendBtn.disabled = false; }
    }
  }

  async function retry() { retryBtn.classList.add("hidden"); setNotice(""); await post("/retry"); }

  // Voice (optional): dictation fills the input box for review; replies can be read aloud.
  // Audio goes to the server, which passes it to the speech provider; nothing is stored.
  const micBtn = document.getElementById("mic-btn");
  const speakToggle = document.getElementById("speak-toggle");
  let recorder = null, recTimer = null, player = null;

  function stopAudio() { if (player) { player.pause(); player = null; } }

  async function speakEntry(entry) {
    if (!speakToggle || !speakToggle.checked || entry.seq == null) return;
    try {
      const r = await fetch(base + "/speak/" + entry.seq, { method: "POST" });
      if (!r.ok) { setNotice("Couldn't read the reply aloud. You can still read it above.", "error"); return; }
      const url = URL.createObjectURL(await r.blob());
      stopAudio();
      player = new Audio(url);
      player.onended = () => URL.revokeObjectURL(url);
      await player.play();
    } catch (_) {}
  }

  // Browsers record WebM/MP4, which the provider doesn't accept: convert to 16 kHz mono WAV.
  async function toWav16k(blob) {
    const ctx = new AudioContext();
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    ctx.close();
    const rate = 16000;
    const off = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
    const src = off.createBufferSource();
    src.buffer = decoded; src.connect(off.destination); src.start();
    const pcm = (await off.startRendering()).getChannelData(0);
    const v = new DataView(new ArrayBuffer(44 + pcm.length * 2));
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE"); str(12, "fmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, "data"); v.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) {
      const x = Math.max(-1, Math.min(1, pcm[i]));
      v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
    }
    return new Blob([v], { type: "audio/wav" });
  }

  async function transcribe(chunks) {
    micBtn.disabled = true; micBtn.textContent = "Transcribing…";
    try {
      const fd = new FormData();
      fd.append("audio", await toWav16k(new Blob(chunks, { type: chunks[0].type })), "answer.wav");
      const r = await fetch(base + "/transcribe", { method: "POST", body: fd });
      let data = {}; try { data = await r.json(); } catch (_) {}
      if (r.ok) {
        input.value = ((input.value.trim() ? input.value.trimEnd() + " " : "") + data.text).slice(0, input.maxLength);
        setNotice(""); input.focus();
      } else if (r.status === 410) close(data.error);
      else setNotice(data.error || "Couldn't transcribe that. Please type your answer.", "error");
    } catch (_) {
      setNotice("Couldn't process the recording. Please type your answer.", "error");
    }
    micBtn.disabled = closed; micBtn.textContent = "🎤 Speak";
  }

  async function toggleMic() {
    if (recorder) { if (recorder.state === "recording") recorder.stop(); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (_) { setNotice("Microphone access was blocked. You can type your answer instead.", "error"); return; }
    stopAudio();  // don't record the interviewer's voice
    const chunks = [], started = Date.now();
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    recorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      clearInterval(recTimer); recorder = null;
      micBtn.setAttribute("aria-pressed", "false"); micBtn.textContent = "🎤 Speak";
      if (!closed && chunks.length) transcribe(chunks);
    };
    recorder.start();
    micBtn.setAttribute("aria-pressed", "true");
    const tick = () => {
      const s = Math.floor((Date.now() - started) / 1000);
      micBtn.textContent = "■ Stop (" + s + "s)";
      if (s >= window.INTERVIEW.maxRecordSeconds && recorder && recorder.state === "recording") recorder.stop();
    };
    tick(); recTimer = setInterval(tick, 250);
  }

  if (micBtn) {
    // Microphone access needs HTTPS (or localhost) and MediaRecorder support.
    if (!navigator.mediaDevices || !window.MediaRecorder || !window.OfflineAudioContext) micBtn.classList.add("hidden");
    else micBtn.addEventListener("click", toggleMic);
  }
  if (speakToggle) {
    try { speakToggle.checked = localStorage.getItem("interview-speak") === "1"; } catch (_) {}
    speakToggle.addEventListener("change", () => {
      try { localStorage.setItem("interview-speak", speakToggle.checked ? "1" : "0"); } catch (_) {}
      if (!speakToggle.checked) stopAudio();
    });
  }

  document.getElementById("composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  retryBtn.addEventListener("click", retry);

  const startBtn = document.getElementById("start-btn");
  if (startBtn) {
    startBtn.addEventListener("click", async () => {
      if (!document.getElementById("consent").checked) {
        document.getElementById("start-error").textContent = "Please tick the box to continue."; return;
      }
      startBtn.disabled = true;
      const r = await post("/start", { consent: true });
      if (!r.ok) { document.getElementById("start-error").textContent = r.data.error || "Could not start."; startBtn.disabled = false; return; }
      document.getElementById("intro").remove();
      document.getElementById("chat").classList.remove("hidden");
      connect(); input.focus();
    });
  } else {
    connect();
  }
})();
