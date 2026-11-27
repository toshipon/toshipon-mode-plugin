(function () {
  "use strict";
  // Every string in `data` came from project sources. It is only ever placed with
  // textContent, value or setAttribute on a fixed attribute, never parsed as markup.
  var data = JSON.parse(document.getElementById("explorer-data").textContent);
  var SVG = "http://www.w3.org/2000/svg";
  var NODE_W = 190, NODE_H = 34, GAP_X = 50, GAP_Y = 12, GAP_ROW = 24, TOP = 26;
  var byId = {}, nodes = {}, lines = [], selected = null;

  function el(tag, text, className) {
    var node = document.createElement(tag);
    if (text !== undefined) { node.textContent = text; }
    if (className) { node.className = className; }
    return node;
  }
  function svg(tag, attributes) {
    var node = document.createElementNS(SVG, tag);
    Object.keys(attributes || {}).forEach(function (key) { node.setAttribute(key, String(attributes[key])); });
    return node;
  }
  function clip(text, length) { return text.length > length ? text.slice(0, length - 1) + "…" : text; }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  data.entities.forEach(function (entity) { byId[entity.id] = entity; });

  function neighbours(id) {
    var found = [];
    data.edges.forEach(function (edge) {
      if (edge[0] === id) { found.push({ id: edge[2], label: edge[1] + " →" }); }
      if (edge[2] === id) { found.push({ id: edge[0], label: "← " + edge[1] }); }
    });
    return found;
  }

  function drawGraph() {
    var box = document.getElementById("graph");
    var columns = data.types.filter(function (type) {
      return data.entities.some(function (entity) { return entity.type === type; });
    });
    // One labelled stack per type, flowing left to right and wrapping at the panel's width.
    var available = Math.max(document.getElementById("graph-box").clientWidth, NODE_W + 20);
    var position = {}, x = 10, top = 0, rowHeight = 0, width = 0;
    columns.forEach(function (type) {
      var members = data.entities.filter(function (entity) { return entity.type === type; });
      if (x > 10 && x + NODE_W > available) { x = 10; top += rowHeight + GAP_ROW; rowHeight = 0; }
      var label = svg("text", { x: x, y: top + 16, "class": "column" });
      label.textContent = type;
      box.appendChild(label);
      members.forEach(function (entity, row) {
        position[entity.id] = { x: x, y: top + TOP + row * (NODE_H + GAP_Y) };
      });
      rowHeight = Math.max(rowHeight, TOP + members.length * (NODE_H + GAP_Y));
      width = Math.max(width, x + NODE_W + 10);
      x += NODE_W + GAP_X;
    });
    var height = top + rowHeight + 10;
    box.setAttribute("viewBox", "0 0 " + width + " " + height);
    box.setAttribute("width", width);
    box.setAttribute("height", height);
    data.edges.forEach(function (edge) {
      var from = position[edge[0]], to = position[edge[2]];
      var x1 = from.x + NODE_W / 2, y1 = from.y + NODE_H / 2, x2 = to.x + NODE_W / 2, y2 = to.y + NODE_H / 2;
      // A curve, so that an edge between distant nodes does not run straight through the ones between.
      var bend = from.x === to.x ? NODE_W * 0.75 : 0, lift = from.x === to.x ? 0 : NODE_H;
      var line = svg("path", {
        d: "M" + x1 + " " + y1 + " Q" + ((x1 + x2) / 2 + bend) + " " + ((y1 + y2) / 2 - lift) + " " + x2 + " " + y2
      });
      var title = svg("title");
      title.textContent = edge[0] + " " + edge[1] + " " + edge[2];
      line.appendChild(title);
      box.appendChild(line);
      lines.push({ line: line, from: edge[0], to: edge[2] });
    });
    data.entities.forEach(function (entity) {
      var at = position[entity.id];
      var group = svg("g", { "class": "node" + (entity.findings.length ? " flagged" : ""), tabindex: 0, role: "button" });
      group.setAttribute("aria-label", entity.id);
      group.appendChild(svg("rect", { x: at.x, y: at.y, width: NODE_W, height: NODE_H, rx: 6 }));
      var label = svg("text", { x: at.x + 8, y: at.y + 21 });
      label.textContent = clip(entity.id, 26);
      group.appendChild(label);
      group.addEventListener("click", function () { select(entity.id); });
      group.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(entity.id); }
      });
      box.appendChild(group);
      nodes[entity.id] = group;
    });
  }

  function entityButton(id, extra) {
    var entity = byId[id];
    var button = el("button", entity.id, "entity");
    button.type = "button";
    button.appendChild(el("small", (extra ? extra + " · " : "") + entity.type + " · " + clip(entity.description, 60)));
    button.addEventListener("click", function () { select(id); });
    return button;
  }

  function matches(entity, query) {
    var text = [entity.id, entity.type, entity.description, entity.config_path || ""].join(" ").toLowerCase();
    return query.split(/\s+/).every(function (word) { return text.indexOf(word) !== -1; });
  }

  function search() {
    var query = document.getElementById("search").value.trim().toLowerCase();
    var results = document.getElementById("results");
    clear(results);
    var shown = 0;
    data.entities.forEach(function (entity) {
      var hit = !query || matches(entity, query);
      nodes[entity.id].classList.toggle("dim", !hit);
      if (!hit) { return; }
      var item = el("li");
      var button = entityButton(entity.id);
      if (entity.id === selected) { button.setAttribute("aria-current", "true"); }
      item.appendChild(button);
      results.appendChild(item);
      shown += 1;
    });
    document.getElementById("count").textContent = shown + " / " + data.entities.length + " 件";
  }

  function disclosure(title, body, open) {
    var details = el("details");
    details.open = Boolean(open);
    details.appendChild(el("summary", title));
    details.appendChild(body);
    return details;
  }

  function pairs(rows) {
    var list = el("dl");
    rows.forEach(function (row) {
      list.appendChild(el("dt", row[0]));
      list.appendChild(el("dd", row[1]));
    });
    return list;
  }

  function select(id) {
    selected = id;
    var entity = byId[id];
    var near = neighbours(id);
    var nearIds = near.map(function (other) { return other.id; });
    Object.keys(nodes).forEach(function (other) {
      nodes[other].classList.toggle("selected", other === id);
      nodes[other].classList.toggle("near", nearIds.indexOf(other) !== -1);
    });
    lines.forEach(function (edge) { edge.line.classList.toggle("active", edge.from === id || edge.to === id); });
    var detail = document.getElementById("detail");
    clear(detail);
    detail.appendChild(el("h2", entity.id));
    detail.appendChild(el("p", entity.type + (entity.status ? " · " + entity.status : ""), "meta"));
    detail.appendChild(el("p", entity.description));

    var related = el("ul");
    near.forEach(function (other) {
      var item = el("li");
      item.appendChild(entityButton(other.id, other.label));
      related.appendChild(item);
    });
    if (!near.length) { related.appendChild(el("li", "関係は記録されていない", "note")); }
    detail.appendChild(disclosure("関係（" + near.length + "）", related, true));

    var source = entity.source || {};
    detail.appendChild(disclosure("出典", pairs([
      ["locator", source.locator || "記録なし"],
      ["revision", source.revision || "記録なし"],
      ["config path", entity.config_path || "-"]
    ]), false));

    var gaps = el("ul");
    entity.findings.forEach(function (finding) { gaps.appendChild(el("li", finding.kind + ": " + finding.detail, "gap")); });
    if (!entity.findings.length) { gaps.appendChild(el("li", "なし", "note")); }
    detail.appendChild(disclosure("欠落と古い参照（" + entity.findings.length + "）", gaps, entity.findings.length > 0));

    var prompt = el("div");
    var area = el("textarea");
    area.readOnly = true;
    area.value = entity.prompt;
    area.setAttribute("aria-label", "context prompt");
    var copy = el("button", "コピー", "copy");
    copy.type = "button";
    var status = el("span", "", "note");
    status.setAttribute("role", "status");
    copy.addEventListener("click", function () {
      function done() { status.textContent = " コピーした"; }
      function fallback() { area.focus(); area.select(); status.textContent = " 選択した。Ctrl+C / ⌘C でコピーする"; }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(area.value).then(done, fallback);
      } else {
        fallback();
      }
    });
    prompt.appendChild(el("p", "新しいセッションに貼る。資料の取得はそのセッションが行う。", "note"));
    prompt.appendChild(area);
    prompt.appendChild(copy);
    prompt.appendChild(status);
    detail.appendChild(disclosure("context prompt", prompt, false));
    search();
  }

  document.getElementById("title").textContent = data.title;
  document.getElementById("summary").textContent =
    data.entities.length + " entities · " + data.edges.length + " relations · " + data.gaps + " gaps · HEAD " +
    data.head.slice(0, 12) + " · " + data.generated_at;
  drawGraph();
  document.getElementById("search").addEventListener("input", search);
  search();
  if (data.entities.length) { select(data.entities[0].id); }
})();
