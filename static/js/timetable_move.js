// Drag and drop for the timetable grid (SortableJS, vendored). The browser only
// reports where a card was dropped; the server decides, and the grid is always
// re-rendered from its answer, so a refused move puts the card back.
(function () {
  "use strict";
  var dragging = false;

  function body() {
    return document.getElementById("timetable-body");
  }

  function reload(grid) {
    // After a failure with no grid in the answer: fetch the current one.
    var url = grid.dataset.moveUrl.replace(/sposta\/$/, "griglia/");
    htmx.ajax("GET", url, { target: "#timetable-body", swap: "outerHTML" });
  }

  function onEnd(evt) {
    dragging = false;
    var grid = body();
    if (!grid || evt.to === evt.from) {
      return;
    }
    var cell = evt.to;
    htmx.ajax("POST", grid.dataset.moveUrl, {
      target: "#timetable-body",
      swap: "outerHTML",
      values: {
        slot_id: evt.item.dataset.slotId,
        giorno: cell.dataset.day,
        ora_inizio: cell.dataset.hour,
        version: grid.dataset.version,
      },
      headers: { "X-CSRFToken": grid.dataset.csrf },
    });
  }

  function init() {
    var grid = body();
    if (!grid || grid.dataset.movable !== "true" || typeof Sortable === "undefined") {
      return;
    }
    grid.querySelectorAll("td[data-drop]").forEach(function (cell) {
      Sortable.create(cell, {
        group: cell.dataset.drop, // a card moves only inside its own class
        draggable: ".tt-draggable",
        animation: 120,
        ghostClass: "tt-ghost",
        onStart: function () {
          dragging = true;
        },
        onEnd: onEnd,
      });
    });
  }

  document.addEventListener("htmx:beforeSwap", function (event) {
    // Do not replace the grid under the user's hand with a poll answer.
    var verb = event.detail.requestConfig && event.detail.requestConfig.verb;
    if (dragging && verb === "get") {
      event.detail.shouldSwap = false;
    }
  });
  document.addEventListener("htmx:afterSettle", function (event) {
    if (event.detail.elt && event.detail.elt.id === "timetable-body") {
      init();
    }
  });
  // 403, 5xx and network errors carry no grid: show the server state again.
  function restore() {
    var grid = body();
    if (grid && grid.dataset.moveUrl) {
      reload(grid);
    }
  }
  document.addEventListener("htmx:responseError", function (event) {
    if (event.detail.requestConfig.verb === "post") {
      restore();
    }
  });
  document.addEventListener("htmx:sendError", restore);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
