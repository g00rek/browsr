import { View, div } from "gpui-kit";
import { h_flex } from "gpui-base";
import { exec, spaces, theme, home, notify } from "herdr";

// Browsr keeps one browser tab group per space, named after it, and brings
// that group forward when you switch spaces.
//
// Following the space is not this panel's job: Browsr's own watcher does it,
// started with the browser, reading the app's view.json. A panel is alive
// only while it is on screen, and the browser has to keep up whether or not
// anyone is looking at one. This is the window onto that: what the browser
// is doing, and the three things worth doing to it by hand.

// Every call says --app. `exec` hands the person's own environment to the
// program, with none of the variables a terminal inside Herdr carries, so
// nothing in it tells bridge.py which Herdr is asking. Without this it can
// read the other one's spaces and prune this one's groups against them.
//
// Where Browsr is checked out. Change this line if yours is elsewhere.
const BRIDGE = `${home()}/Projects/browsr/bridge.py`;

// How often to ask the browser how it is doing. Only the strip's own words
// depend on this; following a space does not.
const STATUS_EVERY = 5000;

export default class Browsr extends View {
  init(_props, cx) {
    this.status = null;
    this.note = "";
    cx.timer.every(STATUS_EVERY, (cx) => this.refresh(cx));
    this.refresh(cx);
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

  act(cx, argument) {
    cx.spawn(async (cx) => {
      try {
        await exec(BRIDGE, ["--app", argument]);
        this.note = "";
      } catch (error) {
        this.note = String(error?.message ?? error);
        notify({ title: "Browsr", body: this.note, level: "error" });
      }
      this.refresh(cx);
    });
  }

  /** What the strip says: the state in one line, in the person's words. */
  words() {
    if (this.note) return this.note;
    if (!this.status) return "asking…";
    if (!this.status.browser) return "not running";
    if (!this.status.bridge) return "running, but its extension is not answering";
    const strip = this.status.strip ?? {};
    const groups = (strip.groups ?? []).length;
    const here = spaces().length;
    if (strip.windows && strip.windows !== 1) return `${strip.windows} windows — there should be one`;
    if (groups !== here) return `${groups} groups, ${here} spaces`;
    return `${groups} groups, one per space`;
  }

  render(cx) {
    const t = theme();
    const ok = this.status?.browser && this.status?.bridge;
    const dot = this.note ? t.blocked : ok ? t.done : t.idle;

    const button = (label, argument, hint) =>
      div()
        .id(`browsr-${argument}`)
        .px(8)
        .py(2)
        .rounded(4)
        .text_color(t.muted)
        .hover((s) => s.bg(t.hover).text_color(t.foreground))
        .tooltip(hint)
        .on_click((_event, cx) => this.act(cx, argument))
        .child(label);

    return h_flex()
      .id("root")
      .size_full()
      .items_center()
      .gap(10)
      .px(10)
      .bg(t.chrome)
      .text_size(12)
      .child(div().size(7).rounded(4).bg(dot))
      .child(div().text_color(t.foreground).child("Browsr"))
      .child(div().flex_1().text_color(t.dim).child(this.words()))
      .child(button("show", "launch", "Bring the browser window forward"))
      .child(button("match", "sync", "Make the groups match the spaces"))
      .child(button("adopt", "adopt", "Take over groups left by the other Herdr"));
  }
}
