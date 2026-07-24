// Point this at wherever your backend is running.
const API_BASE = "http://localhost:8000";

// ---------- Sidebar (mobile) ---------- //
const sidebar = document.getElementById("sidebar");
document.getElementById("openSidebar").addEventListener("click", () => sidebar.classList.add("open"));
document.getElementById("closeSidebar").addEventListener("click", () => sidebar.classList.remove("open"));

// ---------- URL inputs ---------- //
const urlList = document.getElementById("urlList");
const addUrlBtn = document.getElementById("addUrlBtn");
const processBtn = document.getElementById("processBtn");
const statusEl = document.getElementById("status");

addUrlBtn.addEventListener("click", () => {
  if (urlList.children.length >= 5) return;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "url-input";
  input.placeholder = `https://example.com/article`;
  urlList.appendChild(input);
});

processBtn.addEventListener("click", async () => {
  const urls = Array.from(urlList.querySelectorAll(".url-input"))
    .map((i) => i.value.trim())
    .filter(Boolean);

  if (urls.length === 0) {
    setStatus("Please enter at least one valid URL.", "error");
    return;
  }

  processBtn.disabled = true;
  setStatus("⏳ Loading and indexing sources...", "");

  try {
    const res = await fetch(`${API_BASE}/api/process-urls`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls }),
    });
    const data = await res.json();

    if (!res.ok) throw new Error(data.detail || "Failed to process URLs.");

    setStatus(`✅ Indexed ${data.chunks_indexed} chunks from ${data.urls.length} source(s).`, "success");
  } catch (err) {
    setStatus(`❌ ${err.message}`, "error");
  } finally {
    processBtn.disabled = false;
  }
});

function setStatus(text, type) {
  statusEl.textContent = text;
  statusEl.className = `status ${type || ""}`;
}

// ---------- Chat ---------- //
const chatWindow = document.getElementById("chatWindow");
const chatForm = document.getElementById("chatForm");
const chatInput = document.getElementById("chatInput");
const sendBtn = document.getElementById("sendBtn");

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;

  appendMessage("user", message);
  chatInput.value = "";
  sendBtn.disabled = true;

  const assistantBubble = appendMessage("assistant", "");
  let fullText = "";

  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });

    if (!res.ok || !res.body) {
      throw new Error(`Request failed (${res.status})`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop(); // keep incomplete chunk in buffer

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = JSON.parse(line.slice(6));

        if (payload.type === "token") {
          fullText += payload.content;
          renderMarkdown(assistantBubble, fullText);
          chatWindow.scrollTop = chatWindow.scrollHeight;
        } else if (payload.type === "sources" && payload.content.length > 0) {
          appendSources(payload.content);
        } else if (payload.type === "error") {
          fullText += `\n\n⚠️ ${payload.content}`;
          renderMarkdown(assistantBubble, fullText);
        }
      }
    }

    if (!fullText) {
      assistantBubble.textContent = "(No response received.)";
    }
  } catch (err) {
    assistantBubble.textContent = `⚠️ Error: ${err.message}`;
  } finally {
    sendBtn.disabled = false;
  }
});

function appendMessage(role, text) {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;

  const bubble = document.createElement("div");
  bubble.className = "bubble";

  if (role === "assistant" && text) {
    renderMarkdown(bubble, text);
  } else {
    bubble.textContent = text;
  }

  wrapper.appendChild(bubble);
  chatWindow.appendChild(wrapper);
  chatWindow.scrollTop = chatWindow.scrollHeight;

  return bubble;
}

// Renders markdown text into an element as safe-ish HTML.
// User messages stay as textContent (set above) so nothing typed by the
// user is ever interpreted as HTML/markdown.
function renderMarkdown(el, text) {
  if (window.marked) {
    el.innerHTML = marked.parse(text, { breaks: true });
  } else {
    // Fallback if the CDN script hasn't loaded for some reason.
    el.textContent = text;
  }
}

function appendSources(sources) {
  const details = document.createElement("details");
  details.className = "sources";

  const summary = document.createElement("summary");
  summary.textContent = "📌 Referenced Sources";
  details.appendChild(summary);

  sources.forEach((src) => {
    const p = document.createElement("div");
    p.textContent = `- ${src}`;
    details.appendChild(p);
  });

  chatWindow.appendChild(details);
  chatWindow.scrollTop = chatWindow.scrollHeight;
}
