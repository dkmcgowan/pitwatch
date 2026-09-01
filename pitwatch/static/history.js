// The history page.
//
// Three charts drawn as SVG, by hand. A charting library would be a build step
// and a megabyte to render what is, in the end, a few polylines and some
// rectangles, and the content security policy on this application does not let
// a page fetch one anyway.
//
// Drawn at the size the box actually is rather than scaled from a viewBox. A
// viewBox scales the type with it, which on a phone means axis labels at six
// pixels and on a wide screen means them at twenty. Measuring costs a redraw
// on resize and is worth it.

(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const PAD = { left: 34, right: 8, top: 10, bottom: 20 };
  const LABEL_WIDTH = 68;

  // The two pumps, told apart by color and by the key under the chart. Never
  // by color alone: the key names them and the tooltip on each point says
  // which, because a red and a green line are one line to a colorblind reader.
  const SERIES = ["var(--series-1)", "var(--series-2)"];

  // A contact is drawn in the color its lamp would be on the dashboard, so the
  // two pages teach the same thing.
  const CONTACT_COLOR = {
    system_alert: "var(--crit)",
    high_water: "var(--crit)",
    pump1_fault: "var(--crit)",
    pump2_fault: "var(--crit)",
    lead_float: "var(--warn)",
    lag_float: "var(--warn)",
    pump1_run: "var(--ok)",
    pump2_run: "var(--ok)",
  };

  const state = { window: "7d", data: null };

  // -- little helpers -------------------------------------------------------

  function svg(name, attrs) {
    const node = document.createElementNS(NS, name);
    Object.keys(attrs || {}).forEach(function (key) {
      node.setAttribute(key, attrs[key]);
    });
    return node;
  }

  function text(node, value) {
    node.textContent = value;
    return node;
  }

  // A chart is a picture, and a picture needs a name. Without one a screen
  // reader announces "graphic" three times and moves on.
  function box(container, height, label) {
    container.textContent = "";
    const width = Math.max(220, Math.round(container.clientWidth));
    const canvas = svg("svg", {
      width: width,
      height: height,
      role: "img",
      "aria-label": label,
    });
    container.appendChild(canvas);
    return { canvas: canvas, width: width, height: height };
  }

  function at(iso) {
    return new Date(iso).getTime();
  }

  // A round number at or above the highest reading, so the axis reads 0, 5, 10
  // rather than 0, 4.7, 9.4.
  function ceiling(value) {
    if (!(value > 0)) {
      return 1;
    }
    const step = Math.pow(10, Math.floor(Math.log10(value)));
    return Math.ceil(value / step) * step;
  }

  function clock(ms, window_) {
    const when = new Date(ms);
    if (window_ === "24h") {
      return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    return when.toLocaleDateString([], { month: "numeric", day: "numeric" });
  }

  // -- the frame every chart shares -----------------------------------------

  function frame(canvas, width, height, top, options) {
    const plot = {
      left: options.left === undefined ? PAD.left : options.left,
      right: width - PAD.right,
      top: PAD.top,
      bottom: height - PAD.bottom,
    };

    // Three gridlines and their labels. More than that on a chart this size is
    // a grid somebody has to read around rather than against.
    const steps = 2;
    for (let step = 0; step <= steps; step += 1) {
      const value = (top / steps) * step;
      const y = plot.bottom - ((plot.bottom - plot.top) * step) / steps;
      canvas.appendChild(
        svg("line", {
          x1: plot.left,
          x2: plot.right,
          y1: y,
          y2: y,
          stroke: "var(--border)",
          "stroke-width": step === 0 ? 1 : 1,
          "stroke-dasharray": step === 0 ? "" : "2 3",
        })
      );
      canvas.appendChild(
        text(
          svg("text", {
            x: plot.left - 5,
            y: y + 3,
            "text-anchor": "end",
            class: "chart-label",
          }),
          options.format ? options.format(value) : String(Math.round(value))
        )
      );
    }
    return plot;
  }

  function timeAxis(canvas, plot, from, to, window_) {
    const marks = 4;
    for (let mark = 0; mark <= marks; mark += 1) {
      const when = from + ((to - from) / marks) * mark;
      const x = plot.left + ((plot.right - plot.left) / marks) * mark;
      canvas.appendChild(
        text(
          svg("text", {
            x: x,
            y: plot.bottom + 14,
            "text-anchor": mark === 0 ? "start" : mark === marks ? "end" : "middle",
            class: "chart-label",
          }),
          clock(when, window_)
        )
      );
    }
  }

  // -- load -----------------------------------------------------------------

  function drawLoad(container, data) {
    const from = at(data.from);
    const to = at(data.to);
    const numbers = [];
    Object.keys(data.load).forEach(function (number) {
      data.load[number].forEach(function (point) {
        numbers.push(point[1]);
      });
    });
    if (!numbers.length) {
      return false;
    }

    const shape = box(container, 150, "Load over the last " + data.title);
    const top = ceiling(Math.max.apply(null, numbers));
    const plot = frame(shape.canvas, shape.width, shape.height, top, {
      format: function (value) {
        return value.toFixed(value < 10 ? 1 : 0);
      },
    });
    timeAxis(shape.canvas, plot, from, to, data.window);

    const x = function (ms) {
      return plot.left + ((plot.right - plot.left) * (ms - from)) / Math.max(1, to - from);
    };
    const y = function (amps) {
      return plot.bottom - ((plot.bottom - plot.top) * amps) / top;
    };

    // A gap in the readings is drawn as a gap. The meter reports when
    // something changes, so two points an hour apart are not a line between
    // them: joining them would draw an hour of load nobody measured.
    const gap = data.load_bucket * 2500;
    Object.keys(data.load).forEach(function (number, index) {
      const points = data.load[number];
      let path = "";
      let last = null;
      points.forEach(function (point) {
        const ms = at(point[0]);
        const command = last === null || ms - last > gap ? "M" : "L";
        path += command + x(ms).toFixed(1) + " " + y(point[1]).toFixed(1) + " ";
        last = ms;
      });
      if (path) {
        shape.canvas.appendChild(
          svg("path", {
            d: path.trim(),
            fill: "none",
            stroke: SERIES[index % SERIES.length],
            "stroke-width": 1.75,
            "stroke-linejoin": "round",
            "stroke-linecap": "round",
          })
        );
      }
    });
    return true;
  }

  // -- starts ---------------------------------------------------------------

  function drawStarts(container, data) {
    const from = at(data.from);
    const to = at(data.to);
    const counts = [];
    Object.keys(data.starts).forEach(function (number) {
      data.starts[number].forEach(function (point) {
        counts.push(point[1]);
      });
    });
    if (!counts.length) {
      return false;
    }

    const shape = box(container, 130, "Starts over the last " + data.title);
    const top = ceiling(Math.max.apply(null, counts));
    const plot = frame(shape.canvas, shape.width, shape.height, top, {});
    timeAxis(shape.canvas, plot, from, to, data.window);

    const span = Math.max(1, to - from);
    const buckets = Math.max(1, Math.round(span / (data.count_bucket * 1000)));
    const slot = (plot.right - plot.left) / buckets;
    const pumps = Object.keys(data.starts);
    // Two bars to a bucket with a hair between them, and never thinner than a
    // pixel: a day with one start has to be visible next to a day with forty.
    const width = Math.max(1.5, (slot - 2) / pumps.length);

    pumps.forEach(function (number, index) {
      data.starts[number].forEach(function (point) {
        const ms = at(point[0]);
        const left =
          plot.left + ((plot.right - plot.left) * (ms - from)) / span + index * width + 1;
        const height = ((plot.bottom - plot.top) * point[1]) / top;
        shape.canvas.appendChild(
          svg("rect", {
            x: left.toFixed(1),
            y: (plot.bottom - height).toFixed(1),
            width: width.toFixed(1),
            height: Math.max(1, height).toFixed(1),
            fill: SERIES[index % SERIES.length],
          })
        );
      });
    });
    return true;
  }

  // -- contacts -------------------------------------------------------------

  function drawContacts(container, data) {
    const contacts = data.contacts || [];
    if (!contacts.length) {
      return false;
    }

    const from = at(data.from);
    const to = at(data.to);
    const row = 16;
    const shape = box(
      container,
      contacts.length * row + PAD.top + PAD.bottom,
      "The panel contacts over the last " + data.title
    );
    const plot = {
      left: LABEL_WIDTH,
      right: shape.width - PAD.right,
      top: PAD.top,
      bottom: shape.height - PAD.bottom,
    };
    timeAxis(shape.canvas, plot, from, to, data.window);

    const span = Math.max(1, to - from);
    contacts.forEach(function (contact, index) {
      const y = plot.top + index * row;
      shape.canvas.appendChild(
        text(
          svg("text", {
            x: 0,
            y: y + 10,
            class: "chart-label",
          }),
          contact.title
        )
      );
      shape.canvas.appendChild(
        svg("rect", {
          x: plot.left,
          y: y + 2,
          width: Math.max(1, plot.right - plot.left),
          height: row - 6,
          fill: "var(--surface-2)",
          rx: 2,
        })
      );

      contact.spans.forEach(function (pair) {
        const opened = Math.max(from, at(pair[0]));
        const shut = Math.min(to, at(pair[1]));
        const left = plot.left + ((plot.right - plot.left) * (opened - from)) / span;
        // A float that was wet for twenty seconds is a fact about the month. It
        // gets a visible mark rather than a hairline nobody can see.
        const width = Math.max(2, ((plot.right - plot.left) * (shut - opened)) / span);
        shape.canvas.appendChild(
          svg("rect", {
            x: left.toFixed(1),
            y: y + 2,
            width: width.toFixed(1),
            height: row - 6,
            fill: CONTACT_COLOR[contact.role] || "var(--accent)",
            rx: 2,
          })
        );
      });
    });
    return true;
  }

  // -- the page -------------------------------------------------------------

  function key(data) {
    const holder = document.querySelector("[data-key]");
    if (!holder) {
      return;
    }
    holder.textContent = "";
    Object.keys(data.pumps).forEach(function (number, index) {
      const item = document.createElement("span");
      item.className = "key-item";
      const dot = document.createElement("span");
      dot.className = "key-dot";
      dot.style.background = SERIES[index % SERIES.length];
      const name = document.createElement("span");
      name.textContent = data.pumps[number];
      item.appendChild(dot);
      item.appendChild(name);
      holder.appendChild(item);
    });
  }

  function drawAll() {
    const data = state.data;
    if (!data) {
      return;
    }
    const charts = [
      ["load", drawLoad],
      ["starts", drawStarts],
      ["contacts", drawContacts],
    ];
    charts.forEach(function (pair) {
      const container = document.querySelector('[data-chart="' + pair[0] + '"]');
      const empty = document.querySelector('[data-empty="' + pair[0] + '"]');
      if (!container) {
        return;
      }
      // Unhidden before it is drawn, not after. A hidden box measures zero
      // wide, and a chart drawn at zero and then shown is a chart 220 pixels
      // wide in a box twice that.
      container.hidden = false;
      const drawn = pair[1](container, data);
      container.hidden = !drawn;
      if (empty) {
        empty.hidden = drawn;
      }
    });
    key(data);
  }

  async function fetchWindow(name) {
    state.window = name;
    document.querySelectorAll("[data-window]").forEach(function (button) {
      button.classList.toggle("chip-on", button.getAttribute("data-window") === name);
      button.setAttribute("aria-pressed", button.getAttribute("data-window") === name);
    });

    const page = document.querySelector("[data-history]");
    if (page) {
      page.classList.add("loading");
    }
    let payload = null;
    try {
      const response = await fetch("/api/history?window=" + encodeURIComponent(name));
      if (response.ok) {
        payload = await response.json();
      }
    } catch (error) {
      // Left as it was, with the last window still on screen. The page says
      // nothing rather than claiming an empty history.
    }
    if (page) {
      page.classList.remove("loading");
    }
    if (payload) {
      state.data = payload;
      drawAll();
    }
  }

  document.querySelectorAll("[data-window]").forEach(function (button) {
    button.addEventListener("click", function () {
      fetchWindow(button.getAttribute("data-window"));
    });
  });

  // Redrawn at the new size rather than stretched, so the type stays the size
  // it was designed at.
  let resizing = null;
  window.addEventListener("resize", function () {
    window.clearTimeout(resizing);
    resizing = window.setTimeout(drawAll, 150);
  });

  fetchWindow(state.window);
})();
