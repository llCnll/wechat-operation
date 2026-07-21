(() => {
  const panelIndex = (panel) => [...document.querySelectorAll(".metric-primary-column > .metric-panel")].indexOf(panel);

  const replacePanel = async (form, submitter = null) => {
    const panel = form.closest(".metric-panel");
    const index = panelIndex(panel);
    if (index < 0) return;
    const params = new URLSearchParams();
    for (const [name, value] of new FormData(form).entries()) params.append(name, value);
    if (submitter?.name) params.set(submitter.name, submitter.value);
    const response = await fetch(`${location.pathname}?${params.toString()}`, {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    if (!response.ok) return;
    const nextDocument = new DOMParser().parseFromString(await response.text(), "text/html");
    const nextPanel = nextDocument.querySelectorAll(".metric-primary-column > .metric-panel")[index];
    if (!nextPanel) return;
    panel.replaceWith(nextPanel);
    history.replaceState(null, "", `${location.pathname}?${params.toString()}`);
    bindControls(nextPanel);
    bindChartTooltips(nextPanel);
  };

  const bindControls = (root = document) => {
    root.querySelectorAll("form.view-switch, form.metric-comparison-controls").forEach((form) => {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        replacePanel(form, event.submitter);
      });
      if (form.classList.contains("metric-comparison-controls")) {
        form.addEventListener("change", () => replacePanel(form));
      }
    });
  };

  const bindChartTooltips = (root = document) => {
    root.querySelectorAll(".metric-chart").forEach((chart) => {
      if (chart.dataset.tooltipBound) return;
      chart.dataset.tooltipBound = "true";
      const tooltip = document.createElement("div");
      tooltip.className = "chart-tooltip";
      chart.appendChild(tooltip);
      chart.querySelectorAll("svg circle").forEach((point) => {
        const title = point.querySelector("title")?.textContent;
        if (!title) return;
        point.setAttribute("r", "8");
        point.classList.add("chart-hit-point");
        const show = () => {
          tooltip.textContent = title;
          tooltip.classList.add("visible");
          const x = Number(point.getAttribute("cx")) / 720 * chart.clientWidth;
          const y = Number(point.getAttribute("cy")) / 240 * 240;
          tooltip.style.left = `${Math.min(Math.max(x, 80), chart.clientWidth - 80)}px`;
          tooltip.style.top = `${Math.max(y - 38, 12)}px`;
        };
        point.addEventListener("pointerenter", show);
        point.addEventListener("pointerleave", () => tooltip.classList.remove("visible"));
      });
    });
  };

  bindControls();
  bindChartTooltips();
})();
