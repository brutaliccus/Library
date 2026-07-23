import { listLibraries } from "../lib/storage.js";

const libsEl = document.getElementById("libs");
const emptyEl = document.getElementById("empty");

document.getElementById("open-options").addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

async function render() {
  const libraries = await listLibraries();
  libsEl.innerHTML = "";
  emptyEl.classList.toggle("hidden", libraries.length > 0);
  for (const lib of libraries) {
    const li = document.createElement("li");
    li.innerHTML = `<strong></strong><span></span>`;
    li.querySelector("strong").textContent = lib.name;
    li.querySelector("span").textContent = lib.origin;
    libsEl.append(li);
  }
}

render();
