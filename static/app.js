"use strict";

const storageKey = "common.chats.v1";
const $ = (id) => document.getElementById(id);
let conversations = [];
let activeId = null;
let busy = false;
let maxHistory = 20;
try {
  const saved = JSON.parse(localStorage.getItem(storageKey) || "[]");
  if (Array.isArray(saved)) {
    conversations = saved.filter((chat) => chat && typeof chat.id === "string" &&
      typeof chat.title === "string" && Array.isArray(chat.messages)).map((chat) => ({
      ...chat,
      messages: chat.messages.filter((m) => m && ["user", "assistant"].includes(m.role) &&
        typeof m.content === "string" && m.content.length <= (m.role === "user" ? 8000 : 32000)),
    })).slice(0, 30);
  }
} catch { /* Storage may be unavailable in private browsing. */ }

function save() {
  try { localStorage.setItem(storageKey, JSON.stringify(conversations.slice(0, 30))); }
  catch { /* Chat remains usable without persistent storage. */ }
}

function activeChat() { return conversations.find((chat) => chat.id === activeId); }
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  svg.setAttribute("aria-hidden", "true");
  return svg;
}

function renderSidebar() {
  const list = $("conversation-list");
  list.replaceChildren();
  if (!conversations.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = "Your conversations will find a home here.";
    list.append(empty);
  }
  for (const chat of conversations) {
    const button = document.createElement("button");
    button.className = `conversation-item${chat.id === activeId ? " active" : ""}`;
    button.disabled = busy;
    if (chat.id === activeId) button.setAttribute("aria-current", "true");
    const title = document.createElement("span");
    title.textContent = chat.title;
    button.append(icon("chat"), title);
    button.addEventListener("click", () => {
      activeId = chat.id;
      $("error-message").hidden = true;
      render();
      closeSidebar();
    });
    list.append(button);
  }
}

function appendMessage(message) {
  const article = document.createElement("article");
  article.className = `message ${message.role}`;
  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = message.role === "user" ? "You" : "✳";
  const body = document.createElement("div");
  body.className = "message-body";
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = message.role === "user" ? "You" : "Claude";
  if (message.time) {
    const date = new Date(message.time);
    if (!Number.isNaN(date.getTime())) {
      const time = document.createElement("time");
      time.dateTime = date.toISOString();
      time.textContent = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      label.append(time);
    }
  }
  const content = document.createElement("div");
  content.className = "message-content";
  content.textContent = message.content; // Model output and user input are never HTML.
  body.append(label, content);
  if (message.role === "assistant") {
    const copy = document.createElement("button");
    copy.className = "copy-button";
    copy.append(icon("copy"), document.createTextNode("Copy response"));
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(message.content);
        copy.textContent = "Copied";
        setTimeout(() => copy.replaceChildren(icon("copy"), document.createTextNode("Copy response")), 1800);
      } catch { copy.textContent = "Select the text to copy"; }
    });
    body.append(copy);
  }
  article.append(avatar, body);
  $("messages").append(article);
}

function render() {
  renderSidebar();
  const messages = activeChat()?.messages || [];
  $("welcome").hidden = messages.length > 0;
  $("messages").hidden = messages.length === 0;
  $("messages").replaceChildren();
  messages.forEach(appendMessage);
  scrollDown();
}

function scrollDown() {
  requestAnimationFrame(() => { $("scroll-area").scrollTop = $("scroll-area").scrollHeight; });
}

function setBusy(value) {
  busy = value;
  $("send-button").disabled = busy || !$("message-input").value.trim();
  $("new-chat").disabled = busy;
  $("clear-history").disabled = busy;
  $("message-input").disabled = busy;
  document.querySelectorAll(".suggestion").forEach((el) => { el.disabled = busy; });
  renderSidebar();
}

function closeSidebar() {
  $("sidebar").classList.remove("open");
  $("menu-toggle").setAttribute("aria-expanded", "false");
}

async function sendMessage(text) {
  text = text.trim();
  if (!text || busy) return;
  if (text.length > 8000) return;
  $("error-message").hidden = true;
  let chat = activeChat();
  if (!chat) {
    chat = { id: crypto.randomUUID(), title: text.slice(0, 45), messages: [] };
    conversations.unshift(chat);
    conversations = conversations.slice(0, 30);
    activeId = chat.id;
  }
  chat.messages.push({ role: "user", content: text, time: new Date().toISOString() });
  $("message-input").value = "";
  setBusy(true);
  render();
  const typing = document.createElement("div");
  typing.className = "typing";
  typing.setAttribute("role", "status");
  typing.setAttribute("aria-label", "Claude is thinking");
  for (let i = 0; i < 3; i++) typing.append(document.createElement("span"));
  $("messages").append(typing);
  scrollDown();
  try {
    const history = chat.messages.slice(-(maxHistory + 1)).map(({ role, content }) => ({ role, content }));
    // 20 previous messages plus this user turn always starts with a user role.
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history }),
      signal: AbortSignal.timeout(200000),
    });
    let data;
    try { data = await response.json(); }
    catch { throw new Error("The server returned an unexpected response. Please try again."); }
    if (!response.ok) throw new Error(data.error || "Something went wrong. Please try again.");
    if (typeof data.reply !== "string") throw new Error("The server returned an empty response.");
    chat.messages.push({ role: "assistant", content: data.reply, time: new Date().toISOString() });
    save();
  } catch (error) {
    chat.messages.pop(); // Keep the previous valid conversation; restore input for retry.
    $("message-input").value = text;
    $("error-message").textContent = error.name === "TimeoutError" ? "The response took too long. Please try again." : (error.message || "Could not connect. Please try again.");
    $("error-message").hidden = false;
    if (!chat.messages.length) {
      conversations = conversations.filter((item) => item.id !== chat.id);
      activeId = null;
    }
    save();
  } finally {
    setBusy(false);
    render();
    $("message-input").focus();
  }
}

$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage($("message-input").value);
});
$("message-input").addEventListener("input", () => { $("send-button").disabled = busy || !$("message-input").value.trim(); });
$("message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage($("message-input").value);
  }
});
$("new-chat").addEventListener("click", () => {
  if (busy) return;
  activeId = null;
  $("message-input").value = "";
  $("send-button").disabled = true;
  $("error-message").hidden = true;
  render();
  closeSidebar();
  $("message-input").focus();
});
$("clear-history").addEventListener("click", () => {
  if (busy || !conversations.length || !window.confirm("Clear all conversations saved in this browser?")) return;
  conversations = [];
  activeId = null;
  save();
  $("error-message").hidden = true;
  render();
});
$("menu-toggle").addEventListener("click", () => {
  const open = $("sidebar").classList.toggle("open");
  $("menu-toggle").setAttribute("aria-expanded", String(open));
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSidebar(); });
document.querySelectorAll(".suggestion").forEach((button) => {
  button.addEventListener("click", () => sendMessage(button.dataset.prompt));
});
render();
fetch("/api/config").then((response) => {
  if (!response.ok) throw new Error("Unavailable");
  return response.json();
}).then((config) => {
  maxHistory = config.maxHistory;
  $("connection-label").textContent = "Server ready";
  $("connection-label").title = "The server is running. Model access is checked when you send a message.";
}).catch(() => {
  $("connection-label").textContent = "Server offline";
  $("connection-dot").classList.add("offline");
});
