import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initPaneWidths } from "./layout";
import "./styles.css";

initPaneWidths();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
