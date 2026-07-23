import {
  listLibraries,
  upsertLibrary,
  removeLibrary,
  normalizeOrigin,
} from "../lib/storage.js";
import {
  login,
  fetchMe,
  fetchLibraryInfo,
  ensureHostPermission,
  ensureAccessToken,
} from "../lib/api.js";

const form = document.getElementById("connect-form");
const tokenForm = document.getElementById("token-form");
const statusEl = document.getElementById("status");
const listEl = document.getElementById("library-list");
const emptyEl = document.getElementById("empty");
const connectBtn = document.getElementById("connect-btn");

document.getElementById("advanced-toggle").addEventListener("click", () => {
  form.classList.add("hidden");
  tokenForm.classList.remove("hidden");
  clearStatus();
});

document.getElementById("advanced-back").addEventListener("click", () => {
  tokenForm.classList.add("hidden");
  form.classList.remove("hidden");
  clearStatus();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearStatus();
  connectBtn.disabled = true;
  try {
    const origin = normalizeOrigin(document.getElementById("origin").value);
    const email = document.getElementById("email").value.trim();
    const password = document.getElementById("password").value;
    if (!origin) throw new Error("Enter a valid library URL (https://…).");

    await ensureHostPermission(origin);
    const tokens = await login(origin, { email, password });
    const name = await resolveLibraryName(origin, tokens.access_token);

    await upsertLibrary({
      origin,
      name,
      email: tokens.email || email,
      session: {
        access_token: tokens.access_token,
        refresh_token: tokens.refresh_token,
        role: tokens.role,
        username: tokens.username,
        email: tokens.email || email,
      },
    });

    document.getElementById("password").value = "";
    showStatus(`Connected to ${name}`, "ok");
    await refreshList();
    await requestMenuRebuild();
  } catch (err) {
    showStatus(err.message || "Connection failed", "err");
  } finally {
    connectBtn.disabled = false;
  }
});

tokenForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearStatus();
  try {
    const origin = normalizeOrigin(document.getElementById("token-origin").value);
    const access_token = document.getElementById("access-token").value.trim();
    const refresh_token = document.getElementById("refresh-token").value.trim();
    const customName = document.getElementById("token-name").value.trim();
    if (!origin) throw new Error("Enter a valid library URL.");

    await ensureHostPermission(origin);
    const me = await fetchMe(origin, access_token);
    const name = customName || (await resolveLibraryName(origin, access_token));

    await upsertLibrary({
      origin,
      name,
      email: me.email || me.username || "token-user",
      session: {
        access_token,
        refresh_token,
        role: me.role,
        username: me.username,
        email: me.email || null,
      },
    });

    tokenForm.reset();
    showStatus(`Saved session for ${name}`, "ok");
    await refreshList();
    await requestMenuRebuild();
  } catch (err) {
    showStatus(err.message || "Could not save tokens", "err");
  }
});

/**
 * @param {string} origin
 * @param {string} accessToken
 */
async function resolveLibraryName(origin, accessToken) {
  try {
    const info = await fetchLibraryInfo(origin, accessToken);
    const name = info?.library?.name;
    if (name) return name;
  } catch {
    // ignore — library onboarding may be incomplete
  }
  try {
    const host = new URL(origin).hostname;
    return host || "Library";
  } catch {
    return "Library";
  }
}

async function refreshList() {
  const libraries = await listLibraries();
  listEl.innerHTML = "";
  emptyEl.classList.toggle("hidden", libraries.length > 0);

  for (const lib of libraries) {
    const li = document.createElement("li");
    const meta = document.createElement("div");
    meta.className = "lib-meta";
    meta.innerHTML = `<strong></strong><span></span><span></span>`;
    meta.querySelector("strong").textContent = lib.name;
    meta.querySelectorAll("span")[0].textContent = lib.origin;
    meta.querySelectorAll("span")[1].textContent = lib.email
      ? `Signed in as ${lib.email}`
      : `User ${lib.session?.username || ""}`;

    const actions = document.createElement("div");
    actions.className = "row";

    const testBtn = document.createElement("button");
    testBtn.type = "button";
    testBtn.className = "ghost";
    testBtn.textContent = "Test";
    testBtn.addEventListener("click", async () => {
      clearStatus();
      try {
        await ensureHostPermission(lib.origin);
        await ensureAccessToken(lib);
        showStatus(`Session OK for ${lib.name}`, "ok");
        await refreshList();
      } catch (err) {
        showStatus(err.message || "Session check failed", "err");
      }
    });

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "danger";
    removeBtn.textContent = "Disconnect";
    removeBtn.addEventListener("click", async () => {
      if (!confirm(`Disconnect ${lib.name}?`)) return;
      await removeLibrary(lib.id);
      showStatus(`Disconnected ${lib.name}`, "ok");
      await refreshList();
      await requestMenuRebuild();
    });

    actions.append(testBtn, removeBtn);
    li.append(meta, actions);
    listEl.append(li);
  }
}

/** Ask the service worker to rebuild context menus (storage.onChanged is backup). */
async function requestMenuRebuild() {
  try {
    await chrome.runtime.sendMessage({ type: "rebuild-menus" });
  } catch {
    // SW may be waking; storage.onChanged / onStartup will rebuild.
  }
}

function showStatus(message, kind) {
  statusEl.hidden = false;
  statusEl.textContent = message;
  statusEl.className = `status ${kind}`;
}

function clearStatus() {
  statusEl.hidden = true;
  statusEl.textContent = "";
  statusEl.className = "status";
}

refreshList();
