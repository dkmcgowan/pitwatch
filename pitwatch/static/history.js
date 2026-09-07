// The history page.
//
// Four charts and a table, drawn as SVG by hand. A charting library would be a
// build step and a megabyte to render what is, in the end, some rectangles and
// some dots, and the content security policy on this application does not let
// a page fetch one anyway. It also does not allow an inline style attribute,
// which is why nothing here assigns a style property from script: SVG carries
// its colors as presentation attributes, and those are not styles.
//
// Drawn at the size the box actually is rather than scaled from a viewBox. A
// viewBox scales the type with it, which on a phone means axis labels at six
// pixels and on a wide screen means them at twenty. Measuring costs a redraw
// on resize and is worth it.
//
// Every chart can be read with a finger. A line follows the cursor and the
// numbers under that moment appear above the chart, because a shape tells you
// something happened on Tuesday and this is a page somebody opens wanting to
// know what it was.

(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const PAD = { left: 34, right: 8, top: 10, bottom: 20 };

  // The two pumps, told apart by color and by the key under the chart. Never
  // by color alone: the key names them, and the reading that follows the
  // cursor names them again. Only the two per pump charts use these; the ones
  // counting water use the accent, because there is one thing being counted.
  const SERIES = ["var(--series-1)", "var(--series-2)"];

  // The columns of the runs table, by their place in a row from the API.
  const STARTED = 0;
  const PUMP = 1;
  const DURATION = 2;
  const STEADY = 3;
  const ROLE = 4;

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

  // The cursor says more than the axis does: on a week of daily bars the axis
  // reads a date and the moment under a finger is a time as well.
  function moment(ms, window_) {
    const when = new Date(ms);
    const day = when.toLocaleDateString([], {
      weekday: "short",
      month: "numeric",
      day: "numeric",
    });
    if (window_ === "30d") {
      return day;
    }
    return day + " " + when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function amps(value) {
    return value === null || value === undefined ? "--" : value.toFixed(2) + " A";
  }

  // Seconds said the way somebody would say them. Twelve seconds is "12s" and
  // forty minutes is "40 min": a run and the gap between two of them are three
  // orders of magnitude apart and the same formatter has to carry both.
  function spoken(seconds) {
    if (seconds === null || seconds === undefined) {
      return "--";
    }
    if (seconds < 90) {
      return (seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)) + "s";
    }
    const minutes = seconds / 60;
    if (minutes < 90) {
      return Math.round(minutes) + " min";
    }
    const hours = minutes / 60;
    return (hours < 10 ? hours.toFixed(1) : Math.round(hours)) + " hr";
  }

  function hourLabel(hour) {
    if (hour === 0) {
      return "12a";
    }
    if (hour === 12) {
      return "12p";
    }
    return (hour % 12) + (hour < 12 ? "a" : "p");
  }

  function pumpName(data, number) {
    const pump = data.pumps[String(number)];
    return pump ? pump.name : "Pump " + number;
  }

  function pumpIndex(data, number) {
    return Object.keys(data.pumps).indexOf(String(number));
  }

  function median(values) {
    if (!values.length) {
      return null;
    }
    const ordered = values.slice().sort(function (a, b) {
      return a - b;
    });
    const middle = Math.floor(ordered.length / 2);
    return ordered.length % 2
      ? ordered[middle]
      : (ordered[middle - 1] + ordered[middle]) / 2;
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

  // The middle of the window, drawn across it. A cloud of dots says what the
  // spread is; the line says what normal is, which is the thing the next dot
  // gets compared against.
  function medianLine(canvas, plot, y, value, color) {
    if (value === null || value === undefined) {
      return;
    }
    canvas.appendChild(
      svg("line", {
        x1: plot.left,
        x2: plot.right,
        y1: y(value).toFixed(1),
        y2: y(value).toFixed(1),
        stroke: color,
        "stroke-width": 1,
        "stroke-dasharray": "4 4",
        opacity: 0.7,
      })
    );
  }

  // -- the line that follows a finger ---------------------------------------
  //
  // One line per chart, and the numbers under it written above the chart
  // rather than into a tooltip that sits under a thumb. Pointer events, so a
  // mouse and a finger are the same code, and the stylesheet leaves the page
  // free to scroll up and down while a sideways drag reads the chart.

  function wireCursor(shape, plot, name, read) {
    const line = svg("line", {
      y1: plot.top,
      y2: plot.bottom,
      stroke: "var(--text-muted)",
      "stroke-width": 1,
      "stroke-dasharray": "3 3",
      visibility: "hidden",
    });
    shape.canvas.appendChild(line);

    const readout = document.querySelector('[data-readout="' + name + '"]');
    const width = Math.max(1, plot.right - plot.left);

    function show(event) {
      const bounds = shape.canvas.getBoundingClientRect();
      const x = Math.min(plot.right, Math.max(plot.left, event.clientX - bounds.left));
      line.setAttribute("x1", x);
      line.setAttribute("x2", x);
      line.setAttribute("visibility", "visible");
      if (readout) {
        readout.textContent = read((x - plot.left) / width);
      }
    }

    function hide() {
      line.setAttribute("visibility", "hidden");
      if (readout) {
        readout.textContent = "";
      }
    }

    shape.canvas.addEventListener("pointerdown", show);
    shape.canvas.addEventListener("pointermove", show);
    shape.canvas.addEventListener("pointerleave", hide);
    shape.canvas.addEventListener("pointercancel", hide);
  }

  // The point nearest a moment, and nothing at all when the nearest one is
  // further away than the reach. A run half a day from where the finger is is
  // not the run the finger is pointing at.
  function nearest(points, ms, reach) {
    let best = null;
    let distance = reach;
    points.forEach(function (point) {
      const gap = Math.abs(at(point[0]) - ms);
      if (gap <= distance) {
        distance = gap;
        best = point;
      }
    });
    return best;
  }

  // -- calls for water ------------------------------------------------------

  function drawCalls(container, data) {
    const calls = data.calls || [];
    if (!calls.length) {
      return false;
    }

    const from = at(data.from);
    const to = at(data.to);
    const shape = box(container, 140, "Calls for water over the last " + data.title);
    const top = ceiling(
      Math.max.apply(
        null,
        calls.map(function (point) {
          return point[1];
        })
      )
    );
    const plot = frame(shape.canvas, shape.width, shape.height, top, {});
    timeAxis(shape.canvas, plot, from, to, data.window);

    const span = Math.max(1, to - from);
    const buckets = Math.max(1, Math.round(span / (data.count_bucket * 1000)));
    const width = Math.max(2, (plot.right - plot.left) / buckets - 2);
    const y = function (value) {
      return plot.bottom - ((plot.bottom - plot.top) * value) / top;
    };

    calls.forEach(function (point) {
      // A day bucket is stamped at local midnight, which for the first one in
      // the window is before the window opens. Drawn from its own left edge it
      // hangs off the side of the plot as a sliver, so it is clipped to the
      // plot and loses the width it was going to spend outside it.
      const edge = plot.left + ((plot.right - plot.left) * (at(point[0]) - from)) / span;
      const left = Math.max(plot.left, edge) + 1;
      const cut = Math.max(0, plot.left - edge);
      shape.canvas.appendChild(
        svg("rect", {
          x: left.toFixed(1),
          y: y(point[1]).toFixed(1),
          width: Math.max(2, width - cut).toFixed(1),
          height: Math.max(1, plot.bottom - y(point[1])).toFixed(1),
          fill: "var(--accent)",
          rx: 1,
        })
      );
      // The two things worth knowing about a bucket beyond how tall it is,
      // drawn on top of it rather than in a chart of their own: a call that
      // took both pumps, and one that got as far as the high float.
      [
        [point[2], "var(--warn)"],
        [point[3], "var(--crit)"],
      ].forEach(function (mark, index) {
        if (!mark[0]) {
          return;
        }
        shape.canvas.appendChild(
          svg("rect", {
            x: left.toFixed(1),
            y: (y(point[1]) - 5 - index * 4).toFixed(1),
            width: Math.max(2, width - cut).toFixed(1),
            height: 3,
            fill: mark[1],
            rx: 1,
          })
        );
      });
    });

    wireCursor(shape, plot, "calls", function (fraction) {
      const ms = from + fraction * span;
      const point = nearest(calls, ms, data.count_bucket * 1000);
      if (!point) {
        return moment(ms, data.window) + "   no calls";
      }
      const parts = [moment(at(point[0]), data.window), point[1] + " calls"];
      if (point[2]) {
        parts.push(point[2] + " took both pumps");
      }
      if (point[3]) {
        parts.push(point[3] + " reached the high float");
      }
      return parts.join("   ");
    });
    return true;
  }

  // -- run length -----------------------------------------------------------

  function runsOf(data, number) {
    return (data.runs || []).filter(function (run) {
      return run[PUMP] === number && run[DURATION] !== null;
    });
  }

  function drawRuns(container, data) {
    const runs = (data.runs || []).filter(function (run) {
      return run[DURATION] !== null;
    });
    if (!runs.length) {
      return false;
    }

    const from = at(data.from);
    const to = at(data.to);
    const shape = box(container, 150, "How long each run lasted over the last " + data.title);
    const top = ceiling(
      Math.max.apply(
        null,
        runs.map(function (run) {
          return run[DURATION];
        })
      )
    );
    const plot = frame(shape.canvas, shape.width, shape.height, top, {
      format: function (value) {
        return Math.round(value) + "s";
      },
    });
    timeAxis(shape.canvas, plot, from, to, data.window);

    const span = Math.max(1, to - from);
    const x = function (ms) {
      return plot.left + ((plot.right - plot.left) * (ms - from)) / span;
    };
    const y = function (value) {
      return plot.bottom - ((plot.bottom - plot.top) * value) / top;
    };

    Object.keys(data.pumps).forEach(function (number, index) {
      const mine = runsOf(data, Number(number));
      if (!mine.length) {
        return;
      }
      medianLine(
        shape.canvas,
        plot,
        y,
        median(
          mine.map(function (run) {
            return run[DURATION];
          })
        ),
        SERIES[index % SERIES.length]
      );
      mine.forEach(function (run) {
        shape.canvas.appendChild(
          svg("circle", {
            cx: x(at(run[STARTED])).toFixed(1),
            cy: y(run[DURATION]).toFixed(1),
            r: 2.5,
            fill: SERIES[index % SERIES.length],
          })
        );
      });
    });

    wireCursor(shape, plot, "runs", function (fraction) {
      const ms = from + fraction * span;
      const run = nearest(runs, ms, span / 40);
      if (!run) {
        return moment(ms, data.window);
      }
      const parts = [
        moment(at(run[STARTED]), data.window),
        pumpName(data, run[PUMP]) + " ran " + spoken(run[DURATION]),
      ];
      if (run[ROLE] && run[ROLE] !== "unknown") {
        parts.push("as " + run[ROLE]);
      }
      return parts.join("   ");
    });
    return true;
  }

  // -- current draw ---------------------------------------------------------
  //
  // Steady current only. The peak was drawn here as a second faint dot and is
  // gone: on this motor it is the starting surge on every run without
  // exception, so it was a chart of a fact about induction motors rather than
  // about this pump, and being three times the larger number it was the one
  // the eye landed on.

  function drawLoad(container, data) {
    // Only a pump whose clamp has proved it can read current. A channel with
    // no CT fitted reads a convincing zero on every run, and a flat line along
    // the floor is a lie that looks like a healthy measurement.
    const measured = Object.keys(data.pumps).filter(function (number) {
      return data.pumps[number].clamp;
    });
    const drawn = [];
    measured.forEach(function (number) {
      runsOf(data, Number(number)).forEach(function (run) {
        if (run[STEADY] !== null) {
          drawn.push(run);
        }
      });
    });
    if (!drawn.length) {
      return false;
    }

    const from = at(data.from);
    const to = at(data.to);
    const shape = box(container, 150, "What each run drew over the last " + data.title);
    const top = ceiling(
      Math.max.apply(
        null,
        drawn.map(function (run) {
          return run[STEADY];
        })
      )
    );
    const plot = frame(shape.canvas, shape.width, shape.height, top, {
      format: function (value) {
        return value.toFixed(value < 10 ? 1 : 0);
      },
    });
    timeAxis(shape.canvas, plot, from, to, data.window);

    const span = Math.max(1, to - from);
    const x = function (ms) {
      return plot.left + ((plot.right - plot.left) * (ms - from)) / span;
    };
    const y = function (value) {
      return plot.bottom - ((plot.bottom - plot.top) * value) / top;
    };

    measured.forEach(function (number) {
      const index = pumpIndex(data, Number(number));
      const color = SERIES[index % SERIES.length];
      const mine = drawn.filter(function (run) {
        return run[PUMP] === Number(number);
      });
      medianLine(
        shape.canvas,
        plot,
        y,
        median(
          mine.map(function (run) {
            return run[STEADY];
          })
        ),
        color
      );
      mine.forEach(function (run) {
        shape.canvas.appendChild(
          svg("circle", {
            cx: x(at(run[STARTED])).toFixed(1),
            cy: y(run[STEADY]).toFixed(1),
            r: 2.5,
            fill: color,
          })
        );
      });
    });

    wireCursor(shape, plot, "load", function (fraction) {
      const ms = from + fraction * span;
      const run = nearest(drawn, ms, span / 40);
      if (!run) {
        return moment(ms, data.window);
      }
      return [
        moment(at(run[STARTED]), data.window),
        pumpName(data, run[PUMP]),
        "drew " + amps(run[STEADY]),
        "for " + spoken(run[DURATION]),
      ].join("   ");
    });
    return true;
  }

  // -- time of day ----------------------------------------------------------
  //
  // One color and one bar per hour. This was two colors stacked, split by
  // which pump answered, which on an alternating panel comes out half and half
  // in every hour: two colors that meant nothing, with no key under them
  // saying so.

  function drawHours(container, data) {
    const hours = data.hours || [];
    const counts = hours.map(function (hour) {
      return hour[1];
    });
    if (!counts.some(Boolean)) {
      return false;
    }

    const shape = box(container, 140, "Calls by time of day over the last " + data.title);
    const top = ceiling(Math.max.apply(null, counts));
    const plot = frame(shape.canvas, shape.width, shape.height, top, {});

    const slot = (plot.right - plot.left) / 24;
    const width = Math.max(2, slot - 2);
    const y = function (value) {
      return plot.bottom - ((plot.bottom - plot.top) * value) / top;
    };

    hours.forEach(function (hour) {
      const left = plot.left + slot * hour[0] + 1;
      if (hour[1]) {
        shape.canvas.appendChild(
          svg("rect", {
            x: left.toFixed(1),
            y: y(hour[1]).toFixed(1),
            width: width.toFixed(1),
            height: Math.max(1, plot.bottom - y(hour[1])).toFixed(1),
            fill: "var(--accent)",
            rx: 1,
          })
        );
      }
      if (hour[0] % 6 === 0) {
        shape.canvas.appendChild(
          text(
            svg("text", {
              x: (left + width / 2).toFixed(1),
              y: plot.bottom + 14,
              "text-anchor": "middle",
              class: "chart-label",
            }),
            hourLabel(hour[0])
          )
        );
      }
    });

    // The reading is a sentence rather than a pair of numbers, because the
    // thing being asked of this chart is "is that a lot for that hour".
    wireCursor(shape, plot, "hours", function (fraction) {
      const hour = Math.min(23, Math.max(0, Math.floor(fraction * 24)));
      const calls = (hours[hour] || [hour, 0])[1];
      return (
        hourLabel(hour) +
        " to " +
        hourLabel((hour + 1) % 24) +
        "   " +
        calls +
        (calls === 1 ? " call" : " calls") +
        " in " +
        data.title
      );
    });
    return true;
  }

  // -- the figures ----------------------------------------------------------

  // Two lines: the number, and what it is. There was a third for the split
  // between the pumps, which on a phone made one figure three lines tall while
  // the seven beside it were two, so the split moved into the label.
  function figure(holder, label, value) {
    const item = document.createElement("div");
    item.className = "figure";
    const number = document.createElement("span");
    number.className = "figure-value";
    number.textContent = value;
    const name = document.createElement("span");
    name.className = "figure-label";
    name.textContent = label;
    item.appendChild(number);
    item.appendChild(name);
    holder.appendChild(item);
  }

  function renderFigures(data) {
    const holder = document.querySelector("[data-figures]");
    if (!holder) {
      return;
    }
    holder.textContent = "";
    const figures = data.figures || {};
    // In parentheses on the label, rather than on a line of its own. It read
    // "41 / 41 by pump" under the count and wrapped on a phone.
    const split = Object.keys(data.pumps)
      .map(function (number) {
        return data.pumps[number].runs;
      })
      .join(" / ");

    // Calls and runs are both here and are not the same number. One call
    // answered by both pumps is one call and two runs, so runs sits above
    // calls by exactly the count in "took both pumps" further down.
    figure(holder, "calls for water", String(figures.calls || 0));
    figure(holder, split ? "pump runs (" + split + ")" : "pump runs", String(figures.runs || 0));
    figure(holder, "between calls", spoken(figures.typical_gap_s));
    figure(holder, "typical run", spoken(figures.typical_run_s));
    figure(holder, "longest run", spoken(figures.longest_run_s));
    figure(holder, "total run time", spoken(figures.running_s));
    // Two counts that are usually zero, and a zero here is the answer somebody
    // came for rather than an empty box.
    figure(holder, "took both pumps", String(figures.both_ran || 0));
    figure(holder, "reached the high float", String(figures.high_water || 0));

    // Every number above is over the chosen window and none of them says so on
    // its own. "17 min total run time" reads as today unless something nearby
    // says otherwise, and the chips at the top of the page read as a control
    // rather than as a fact about what is on screen.
    const heading = document.querySelector("[data-window-title]");
    if (heading) {
      heading.textContent = "The last " + data.title;
    }
  }

  // -- the table ------------------------------------------------------------

  function cell(row, value, className) {
    const node = document.createElement("td");
    node.textContent = value;
    if (className) {
      node.className = className;
    }
    row.appendChild(node);
    return node;
  }

  // Short enough not to wrap in a narrow column: no weekday, and the hour
  // without a leading zero. "9/6 2:14 PM" rather than "Sat, 9/6 02:14 PM".
  function stamp(ms) {
    const when = new Date(ms);
    return (
      when.toLocaleDateString([], { month: "numeric", day: "numeric" }) +
      " " +
      when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    );
  }

  function renderTable(data) {
    const body = document.querySelector("[data-runs-body]");
    const empty = document.querySelector('[data-empty="table"]');
    if (!body) {
      return;
    }
    body.textContent = "";
    const runs = (data.runs || []).slice(-(data.recent || 20)).reverse();
    if (empty) {
      empty.hidden = runs.length > 0;
    }
    runs.forEach(function (run) {
      const row = document.createElement("tr");
      cell(row, stamp(at(run[STARTED])), "nowrap");
      // The number on its own under a heading that already says Pump. Lead is
      // said by not saying it: on a duplex panel answering one call with one
      // pump every run is the lead one, and the word repeated two hundred
      // times down a column says nothing. Lag is rare and is worth the space.
      cell(row, String(run[PUMP]) + (run[ROLE] === "lag" ? " lag" : ""), "nowrap");
      cell(row, run[DURATION] === null ? "running" : spoken(run[DURATION]), "num");
      // Steady current, and nothing about the starting surge. The column was
      // "15.4 / 39.1" and the second number was the inrush every time.
      cell(row, run[STEADY] === null ? "--" : run[STEADY].toFixed(1), "num");
      body.appendChild(row);
    });
  }

  // -- the page -------------------------------------------------------------

  // The key dot is an SVG square rather than a colored span. The content
  // security policy forbids inline styles, so a script coloring a span in is
  // blocked and the dot renders colorless; a fill attribute is not a style.
  function key(data, name, only) {
    const holder = document.querySelector('[data-key="' + name + '"]');
    if (!holder) {
      return;
    }
    holder.textContent = "";
    Object.keys(data.pumps).forEach(function (number, index) {
      if (only && !only(number)) {
        return;
      }
      const item = document.createElement("span");
      item.className = "key-item";
      const dot = svg("svg", {
        viewBox: "0 0 10 10",
        class: "key-dot",
        "aria-hidden": "true",
      });
      dot.appendChild(
        svg("rect", { width: 10, height: 10, rx: 2, fill: SERIES[index % SERIES.length] })
      );
      const named = document.createElement("span");
      named.textContent = data.pumps[number].name;
      item.appendChild(dot);
      item.appendChild(named);
      holder.appendChild(item);
    });
  }

  function drawAll() {
    const data = state.data;
    if (!data) {
      return;
    }
    const charts = [
      ["calls", drawCalls],
      ["runs", drawRuns],
      ["load", drawLoad],
      ["hours", drawHours],
    ];
    charts.forEach(function (pair) {
      const container = document.querySelector('[data-chart="' + pair[0] + '"]');
      const empty = document.querySelector('[data-empty="' + pair[0] + '"]');
      const readout = document.querySelector('[data-readout="' + pair[0] + '"]');
      if (!container) {
        return;
      }
      if (readout) {
        readout.textContent = "";
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
    renderFigures(data);
    renderTable(data);
    key(data, "runs");
    key(data, "load", function (number) {
      return data.pumps[number].clamp;
    });
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
