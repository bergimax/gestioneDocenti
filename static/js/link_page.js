// Let htmx show the error messages the teacher page returns with 4xx status.
document.addEventListener("htmx:beforeSwap", function (event) {
  var status = event.detail.xhr.status;
  if (status === 400 || status === 404 || status === 429) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
});
