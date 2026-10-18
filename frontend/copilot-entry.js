/* The bundle entry: expose the real PageAgent class on the window,
 * exactly as the library's own global declaration does
 * (Window.PageAgent). Our bridge (static/copilot.js) constructs it
 * from there. Nothing of the library is copied or modified — this
 * file only re-exports the pinned npm package. */
import { PageAgent } from "page-agent";

window.PageAgent = PageAgent;
