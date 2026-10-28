"use strict";
(() => {
  const base = "/sandbox/procurement";
  const root = document.getElementById("content");
  const params = new URLSearchParams(location.search);
  const esc = val => String(val ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[ch]));
  const money = val => `CNY ${(val / 100).toFixed(2)}`;
  const labels = {color:"Color", wireless:"Connection", quiet:"Quiet", multi:"Multi-device", compact:"Compact", light:"Lightweight", mic:"Microphone"};
  const icons = {keyboard:"⌨", mouse:"◉", headset:"♫"};
  const catname = {keyboard:"Keyboard", mouse:"Mouse", headset:"Headset"};
  let known = 0;
  try {
    if (params.has("sid") || params.has("task")) sessionStorage.removeItem("shoptrace.proc.count");
    known = Number(sessionStorage.getItem("shoptrace.proc.count") || 0);
  } catch (_) { /* Storage is optional. */ }
  params.delete("sid");
  params.delete("task");
  history.replaceState(null, "", location.pathname + (params.size ? `?${params}` : ""));
  const busy = val => root.setAttribute("aria-busy", String(val));
  const count = val => {
    known = val;
    document.getElementById("cart-count").textContent = val;
    try { sessionStorage.setItem("shoptrace.proc.count", String(val)); } catch (_) { /* Optional. */ }
  };
  count(Number.isFinite(known) ? known : 0);
  const link = (path, data = {}) => {
    const query = new URLSearchParams(params);
    query.delete("id");
    for (const [key, val] of Object.entries(data)) query.set(key, val);
    return `${base}${path}${query.size ? `?${query}` : ""}`;
  };
  document.getElementById("browse-link").href = link("/");
  document.getElementById("brand-link").href = link("/");
  document.getElementById("cart-link").href = link("/cart");
  async function api(path, body) {
    const resp = await fetch(base + "/api" + path, {
      method:body ? "POST" : "GET", cache:"no-store",
      headers:body ? {"Content-Type":"application/json"} : {},
      ...(body ? {body:JSON.stringify(body)} : {})
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The request could not be completed.");
    return data;
  }
  const pic = row => `<div class="illustration ${esc(row.category)}" aria-hidden="true"><span>${icons[row.category] || "◇"}</span><small>SHOPTRACE / ${esc(catname[row.category])}</small></div>`;
  const facts = row => `<dl class="facts">${Object.entries(row.attrs).map(([key, val]) => `<div><dt>${esc(labels[key] || key)}</dt><dd>${esc(key === "wireless" ? (val ? "Wireless" : "Wired") : typeof val === "boolean" ? (val ? "Yes" : "No") : val)}</dd></div>`).join("")}<div><dt>Stock</dt><dd>${row.stock > 0 ? `${row.stock} available` : "Out of stock"}</dd></div></dl>`;
  function fail(error) {
    root.innerHTML = `<section class="empty" role="alert"><h1>We could not load this page.</h1><p>${esc(error.message)}</p><button class="button secondary" id="reload-page" type="button">Reload page</button></section>`;
    document.getElementById("reload-page").addEventListener("click", () => location.reload());
  }
  async function catalog() {
    const query = new URLSearchParams({q:params.get("q") || "", category:params.get("category") || "", page:params.get("page") || "1"});
    const data = await api("/catalog?" + query);
    root.innerHTML = `<section class="hero"><p class="eyebrow">THE DESK COLLECTION</p><h1>Build a better working day.</h1><p>Compare keyboards, mice, and headsets. Open each product to check its specifications and stock.</p></section>
      <form class="search-form procurement-search" action="${base}/" method="get"><label>Search products<input name="q" aria-label="Search products" type="search" maxlength="120" value="${esc(data.q)}" placeholder="Product name or category"></label><label>Category<select name="category" aria-label="Category"><option value="">All categories</option>${Object.entries(catname).map(([val, name]) => `<option value="${val}"${data.category === val ? " selected" : ""}>${name}</option>`).join("")}</select></label><button class="button primary" type="submit">Search</button></form>
      <section id="results" data-total-results="${data.total}"><div class="result-heading"><h2>${data.total} products</h2><p>Page ${data.page} of ${data.pages} · Four products per page</p></div><div class="product-grid">${data.products.map(row => `<article class="product-card">${pic(row)}<div class="card-body"><span class="category">${esc(catname[row.category])}</span><a class="product-title" href="${esc(link("/product", {id:row.id}))}">${esc(row.name)}</a><strong class="price">${money(row.cents)}</strong><p>Specifications and availability are on the product page.</p><a class="button secondary" href="${esc(link("/product", {id:row.id}))}" aria-label="View ${esc(row.name)} details">View details →</a></div></article>`).join("")}</div>${data.total ? "" : `<div class="empty"><h2>No products found.</h2><p>Try another name or category.</p></div>`}
      <nav class="pagination" aria-label="Catalog pages">${data.page > 1 ? `<a class="button secondary" href="${esc(link("/", {page:data.page - 1}))}">Previous page</a>` : ""}<span>Page ${data.page} of ${data.pages}</span>${data.page < data.pages ? `<a class="button secondary" href="${esc(link("/", {page:data.page + 1}))}">Next page</a>` : ""}</nav></section>`;
    root.querySelector("form").addEventListener("submit", () => busy(true));
  }
  async function detail() {
    const pid = params.get("id") || "";
    const row = await api("/item/" + encodeURIComponent(pid));
    document.title = `${row.name} · ShopTrace Store`;
    root.innerHTML = `<a class="back-link" href="${esc(link("/"))}">← Back to results</a><article id="product-details" class="product-details" data-product-id="${esc(row.id)}">${pic(row)}<div class="detail-body"><span class="category">${esc(catname[row.category])}</span><h1>${esc(row.name)}</h1><p>Check the specifications against your shopping requirements.</p><strong class="detail-price">${money(row.cents)}</strong><section class="fact-section"><h2>Specifications</h2>${facts(row)}</section><div class="detail-actions"><button class="button primary" id="add-item" type="button"${row.stock ? "" : " disabled"}>Add to list</button><a class="button secondary" href="${esc(link("/cart"))}">View list</a></div><p class="add-status" id="add-status" role="status"></p></div></article>`;
    const button = document.getElementById("add-item");
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      button.disabled = true;
      busy(true);
      const status = document.getElementById("add-status");
      status.textContent = "Adding item…";
      try {
        const data = await api("/add", {id:pid});
        status.textContent = data.message;
        status.dataset.state = data.state;
        if (data.ok) count(data.count);
        if (data.state !== "stockout") button.disabled = false;
        else {
          const dl = document.querySelector(".facts");
          dl.lastElementChild.lastElementChild.textContent = "Out of stock";
        }
      } catch (_) {
        status.textContent = "Add result not confirmed. Your shopping list may have changed.";
        status.dataset.state = "uncertain";
        button.disabled = false;
      } finally { busy(false); }
    });
  }
  async function cart() {
    const data = await api("/cart");
    count(data.count);
    document.title = "Shopping list · ShopTrace Store";
    root.innerHTML = `<section class="hero"><p class="eyebrow">YOUR SELECTION</p><h1>Check the whole list.</h1><p>Review specifications, quantities, and the combined price.</p></section><section id="proc-cart"><div class="cart-list">${data.items.map(row => `<article class="cart-item proc-item" data-id="${row.id}" data-qty="${row.qty}" data-cents="${row.cents}">${pic(row)}<div class="cart-item-body"><span class="category">${esc(catname[row.category])}</span><a class="product-title" href="${esc(link("/product", {id:row.id}))}">${esc(row.name)}</a>${facts(row)}<p class="quantity">Quantity: <strong>${row.qty}</strong></p><div class="qty-actions"><button class="button secondary" type="button" data-id="${row.id}" data-delta="-1" aria-label="Decrease ${esc(row.name)} quantity">−</button><button class="button secondary" type="button" data-id="${row.id}" data-delta="1" aria-label="Increase ${esc(row.name)} quantity"${row.qty >= row.stock ? " disabled" : ""}>+</button><button class="text-button" type="button" data-remove="${row.id}" aria-label="Remove ${esc(row.name)}">Remove</button></div></div><div class="cart-item-end"><strong class="price">${money(row.cents * row.qty)}</strong><p>${money(row.cents)} each</p></div></article>`).join("")}</div>${data.items.length ? "" : `<div class="empty"><h2>Your shopping list is empty.</h2><p>Open product details to add an item.</p></div>`}<div class="cart-summary"><p>${data.count} ${data.count === 1 ? "item" : "items"} · Total <strong id="cart-total" data-cents="${data.total}">${money(data.total)}</strong></p><a class="button secondary" href="${esc(link("/"))}">Continue browsing</a></div><p id="cart-status" class="add-status" role="status"></p></section>`;
    for (const btn of root.querySelectorAll("button[data-id],button[data-remove]")) {
      btn.addEventListener("click", async () => {
        if (root.getAttribute("aria-busy") === "true") return;
        busy(true);
        for (const el of root.querySelectorAll("button")) el.disabled = true;
        try {
          const id = btn.dataset.id || btn.dataset.remove;
          const data = await api(btn.dataset.remove ? "/remove" : "/cart", btn.dataset.remove ? {id} : {id, delta:Number(btn.dataset.delta)});
          await cart();
          document.getElementById("cart-status").textContent = data.message;
        } catch (_) {
          document.getElementById("cart-status").textContent = "Update result not confirmed. Reload the list to check its current state.";
          const retry = document.createElement("button");
          retry.className = "button secondary";
          retry.type = "button";
          retry.textContent = "Reload list";
          retry.addEventListener("click", () => location.reload());
          root.append(retry);
        } finally { busy(false); }
      });
    }
  }
  document.addEventListener("click", event => {
    const node = event.target.closest("a[href]");
    if (node && !event.ctrlKey && !event.metaKey && !event.shiftKey) busy(true);
  });
  (async () => {
    try {
      if (location.pathname === base + "/product") await detail();
      else if (location.pathname === base + "/cart") await cart();
      else await catalog();
    } catch (error) { fail(error); }
    finally { busy(false); }
  })();
})();
