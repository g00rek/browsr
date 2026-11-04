import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { exec, spaces, focused, theme, home, notify, run, props, shared, setShared, closeOverlay } from "herdr";

// Browsr keeps one browser tab group per space, named after it, and brings
// that group forward when you switch spaces.
//
// Following the space is not this mod's job: Browsr's own watcher does it,
// started with the browser, reading the app's view.json. A mod is alive only
// while it is on screen, and the browser has to keep up whether or not
// anyone is looking at a panel.
//
// Two roles, one mod, chosen by props:
//   { "role": "button" }  a square in the chrome that opens the panel
//   anything else         the panel: state, actions, and which spaces get a
//                         browser group at all

// Every call says --app. `exec` hands the person's own environment to the
// program, with none of the variables a terminal inside Herdr carries, so
// nothing in it tells bridge.py which Herdr is asking. Without this it can
// read the other one's spaces and prune this one's groups against them.
//
// Where Browsr is checked out. Change this line if yours is elsewhere.
const BRIDGE = `${home()}/Projects/browsr/bridge.py`;

// Which spaces the person has turned Browsr off for: { [spaceId]: true }.
// bridge.py reads it straight out of the app's shared state, so the watcher
// and the panel cannot come to disagree about it.
const SKIP = "browsr.skip";

// How often to ask the browser how it is doing, while the panel is open.
const STATUS_EVERY = 4000;

// Whether the panel is on screen, and when that last changed:
// { open: boolean, changed: <ms> }. The button needs it to answer one
// question — see `pressedWhileOpen` below.
const PANEL = "browsr.panel";

// A press beside a popup dismisses it before the click arrives, and the
// panel's own teardown may land either side of that click. Anything that
// happened this recently is the same press.
const SAME_PRESS = 250;

export default class Browsr extends View {
  init(_props, cx) {
    this.status = null;
    this.note = "";
    this.working = null;
    if (!this.isButton()) {
      setShared(PANEL, { open: true, changed: Date.now() });
      cx.timer.every(STATUS_EVERY, (cx) => this.refresh(cx));
      this.refresh(cx);
    }
  }

  async close() {
    if (!this.isButton()) setShared(PANEL, { open: false, changed: Date.now() });
  }

  /**
   * Was the panel on screen when this press began?
   *
   * The button lives outside the popup, so pressing it dismisses the popup
   * first and only then delivers the click — which would reopen what the
   * person just closed, and the panel would never shut. Two things can be
   * true by the time the click lands: the panel is still there (nothing
   * dismissed it), or it has only just gone. Both mean the press was a
   * "close", so neither may reopen it.
   */
  pressedWhileOpen() {
    const panel = shared(PANEL) ?? {};
    return panel.open === true || Date.now() - (panel.changed ?? 0) < SAME_PRESS;
  }

  isButton() {
    return props()?.role === "button";
  }

  skipped() {
    const chosen = shared(SKIP);
    return chosen && typeof chosen === "object" ? chosen : {};
  }

  /** Ask bridge.py what the browser and the strip of groups look like. */
  refresh(cx) {
    cx.spawn(async (cx) => {
      try {
        const r = await exec(BRIDGE, ["--app", "status"]);
        this.status = JSON.parse(r.stdout);
        this.note = "";
      } catch (error) {
        this.status = null;
        this.note = String(error?.message ?? error);
      }
      cx.notify();
    });
  }

  /** Run one of bridge.py's own commands and report how it went. */
  act(cx, argument, label) {
    if (this.working) return;
    this.working = label;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        await exec(BRIDGE, ["--app", argument]);
        this.note = "";
      } catch (error) {
        this.note = String(error?.message ?? error);
        notify({ title: "Browsr", body: this.note, level: "error" });
      }
      this.working = null;
      this.refresh(cx);
    });
  }

  /** How many projects are turned off right now. */
  offCount() {
    const off = this.skipped();
    return spaces().filter((space) => off[space.id]).length;
  }

  /** Give every project a browser group again. */
  allOn(cx) {
    setShared(SKIP, {});
    this.act(cx, "sync", "every project on");
  }

  /** Turn Browsr on or off for one space, and let the strip follow at once. */
  toggle(cx, space) {
    const chosen = { ...this.skipped() };
    if (chosen[space]) delete chosen[space];
    else chosen[space] = true;
    setShared(SKIP, chosen);
    this.act(cx, "sync", "match");
  }

  /** The state in one line, in the person's words, not the machinery's. */
  words() {
    if (this.note) return this.note;
    if (!this.status) return "asking…";
    if (!this.status.browser) return "not running";
    if (!this.status.bridge) return "running, but its extension is not answering";
    // The browser outlives any one Herdr, and the part that follows your
    // spaces is a separate program. It can be gone while everything else
    // looks fine, and then nothing moves when you switch.
    if (this.status.following === false) return "running, but nothing is following your spaces";
    // The commonest surprise by far: the project you are looking at is
    // turned off below, so switching to it moves nothing and the browser
    // keeps showing whatever came before. Silence looks exactly like a
    // fault, so say it.
    const here = focused()?.space;
    if (here && this.skipped()[here]) return "this project is turned off below — nothing follows here";
    const strip = this.status.strip ?? {};
    if (strip.windows && strip.windows !== 1) return `${strip.windows} windows — there should be one`;
    const groups = (strip.groups ?? []).length;
    const off = this.skipped();
    const wanted = spaces().filter((space) => !off[space.id]).length;
    if (groups !== wanted) return `${groups} groups, ${wanted} spaces want one`;
    return `${groups} groups, one per space`;
  }

  colour(t) {
    if (this.note) return t.blocked;
    if (!this.status) return t.unknown;
    return this.status.browser && this.status.bridge ? t.done : t.idle;
  }

  renderButton() {
    const t = theme();
    return h_flex()
      .id("browsr-button")
      .size_full()
      .items_center()
      .justify_center()
      .bg(t.chrome)
      .tooltip("Browsr")
      .hover((s) => s.bg(t.hover))
      .on_mouse_down("left", () => { this.closing = this.pressedWhileOpen(); })
      .on_click(() => {
        const open = (shared(PANEL) ?? {}).open === true;
        const closing = this.closing;
        this.closing = false;
        // Still there: close it. Already gone with this same press: leave it.
        if (!open && closing) return;
        run("mod.open", { name: "browsr" });
      })
      // One glyph, tinted by how the browser is: green when it is up and
      // answering, grey when it is not running, amber when something is
      // wrong. A separate status dot beside it would say the same thing
      // twice in a button 38px wide.
      .child(svg("mods/browsr/browsr.svg").size(16).text_color(this.colour(t)));
  }

  render(cx) {
    if (this.isButton()) return this.renderButton();
    const t = theme();
    const off = this.skipped();

    // Each label names what it acts on, and says what will happen rather
    // than what the machinery is called. "match" on its own reads as a
    // setting, not as an action with a consequence.
    const action = (label, argument, hint) =>
      div()
        .id(`browsr-${argument}`)
        .px(9)
        .py(3)
        .rounded(5)
        .bg(t.surface)
        .text_color(this.working === label ? t.dim : t.foreground)
        .hover((s) => s.bg(t.hover))
        .tooltip(hint)
        .on_click((_event, cx) => this.act(cx, argument, label))
        .child(this.working === label ? "…" : label);

    const row = (space) => {
      const on = !off[space.id];
      return h_flex()
        .id(`browsr-space-${space.id}`)
        .items_center()
        .gap(9)
        .px(8)
        .py(3)
        .rounded(5)
        .hover((s) => s.bg(t.hover))
        .on_click((_event, cx) => this.toggle(cx, space.id))
        // A tick, or the room one would take. A drawn box with a glyph
        // inside came out as a smudge at this size and took its colour from
        // whatever showed through the panel; this is how the app marks a
        // chosen row everywhere else.
        .child(
          h_flex()
            .size(15)
            .flex_none()
            .items_center()
            .justify_center()
            .when(on, (el) =>
              el.child(svg("mods/browsr/check.svg").size(13).text_color(t.accent)))
        )
        .child(div().flex_1().text_color(on ? t.foreground : t.dim).child(space.name ?? space.id))
        .child(div().text_color(t.dim).text_size(11).child(on ? "" : "no browser"));
    };

    return v_flex()
      .id("root")
      .size_full()
      .bg(t.popover)
      // A press anywhere else puts the panel away, which is what a panel
      // that drops out of a button should do. Said here rather than left to
      // the window, so that it is the same however the panel was opened.
      .on_mouse_down_out(() => {
        const overlay = props()?.overlay;
        if (overlay) closeOverlay(overlay);
      })
      .p(12)
      .gap(10)
      .text_size(13)
      .child(
        h_flex()
          .items_center()
          .gap(8)
          .child(div().size(8).rounded(4).bg(this.colour(t)))
          .child(div().text_color(t.foreground).child("Browsr"))
          .child(div().flex_1().text_color(t.dim).child(this.words()))
      )
      .child(
        // Wrapping, because the fifth button only appears sometimes and a
        // row that silently runs past the edge hides it altogether.
        h_flex()
          .flex_wrap()
          .gap(6)
          .child(action("show browser", "launch",
            "Bring the browser window forward. The only thing that starts it."))
          .child(action("redo groups", "sync",
            "Make the groups match this list: add what is missing, take away what is not wanted."))
          .child(action("restart browser", "restart",
            "Close it and start it again. The only way it reads a changed extension."))
          .child(action("close browser", "quit",
            "Quit the browser. The tabs come back when you show it again."))
          // Only while something is off, and it says how much it will undo.
          .when(this.offCount() > 0, (el) => el.child(
            div()
              .id("browsr-all-on")
              .px(9).py(3).rounded(5)
              .bg(t.surface)
              .text_color(this.working === "every project on" ? t.dim : t.foreground)
              .hover((s) => s.bg(t.hover))
              .tooltip("Give all of them a browser group again.")
              .on_click((_event, cx) => this.allOn(cx))
              .child(this.working === "every project on"
                ? "…" : `turn on ${this.offCount()} off`)))
          .child(action("take over old groups", "adopt",
            "Once, after moving from the Herdr that runs in a terminal: "
            + "the groups it still owns are taken over instead of being made again beside them."))
      )
      .child(div().text_color(t.dim).text_size(11).child("A browser group for:"))
      .child(v_flex().gap(1).children(spaces().map(row)));
  }
}
