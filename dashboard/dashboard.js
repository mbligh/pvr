"use strict";
/* Probe unknown-quality installed videos only when they scroll into view. */
const pending = document.querySelectorAll("td[data-quality-url]");
const inspect = async (cell) => {
  const url = cell.dataset.qualityUrl;
  delete cell.dataset.qualityUrl;
  try {
    const response = await fetch(url, {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) throw new Error("Quality lookup failed");
    const data = await response.json();
    cell.textContent = data.installed ? data.quality : "Not installed";
    cell.classList.remove("waiting");
  } catch (_) {
    cell.textContent = "Quality unavailable";
  }
};
if ("IntersectionObserver" in window) {
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        observer.unobserve(entry.target);
        inspect(entry.target);
      }
    }
  }, {rootMargin: "160px"});
  pending.forEach((cell) => observer.observe(cell));
} else {
  pending.forEach((cell) => inspect(cell));
}
