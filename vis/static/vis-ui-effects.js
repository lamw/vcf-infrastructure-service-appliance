(function () {
  const root = document.documentElement;
  const triggers = Array.from(document.querySelectorAll(".brand-mark"));
  const storageKey = "vis-display-mode";
  const modes = ["", "grid"];
  const labels = {
    "": "standard",
    grid: "enhanced",
  };

  function normalized(value) {
    return modes.includes(value) ? value : "";
  }

  function activeMode() {
    return normalized(root.dataset.visual || "");
  }

  function setMode(value) {
    const mode = normalized(value);
    if (mode) {
      root.dataset.visual = mode;
      window.localStorage.setItem(storageKey, mode);
    } else {
      delete root.dataset.visual;
      window.localStorage.removeItem(storageKey);
    }
    triggers.forEach((trigger) => {
      trigger.setAttribute("aria-label", "Toggle VIS display mode. Current: " + labels[mode]);
      trigger.setAttribute("title", "Toggle VIS display mode");
    });
  }

  function cycleMode() {
    const currentIndex = modes.indexOf(activeMode());
    setMode(modes[(currentIndex + 1) % modes.length]);
  }

  triggers.forEach((trigger) => {
    trigger.addEventListener("click", cycleMode);
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        cycleMode();
      }
    });
  });

  setMode(window.localStorage.getItem(storageKey) || "");
}());
