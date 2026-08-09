fetch("/api/status").then((response) => response.json()).then((value) => {
  document.querySelector("#status").textContent = JSON.stringify(value, null, 2);
}).catch(() => { document.querySelector("#status").textContent = "Status unavailable"; });
