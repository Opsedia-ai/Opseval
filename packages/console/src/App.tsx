import { Route, Routes } from "react-router-dom";
import Shell from "./components/Shell";
import BulkImport from "./pages/BulkImport";
import Checks from "./pages/Checks";
import Compare from "./pages/Compare";
import NewRun from "./pages/NewRun";
import RunDetail from "./pages/RunDetail";
import Runs from "./pages/Runs";
import Scenarios from "./pages/Scenarios";
import Settings from "./pages/Settings";
import Simulator from "./pages/Simulator";
import Suites from "./pages/Suites";
import Trend from "./pages/Trend";
import Usage from "./pages/Usage";

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Runs />} />
        <Route path="runs/new" element={<NewRun />} />
        <Route path="runs/:id" element={<RunDetail />} />
        <Route path="compare" element={<Compare />} />
        <Route path="trend" element={<Trend />} />
        <Route path="usage" element={<Usage />} />
        <Route path="simulator" element={<Simulator />} />
        <Route path="scenarios" element={<Scenarios />} />
        <Route path="import" element={<BulkImport />} />
        <Route path="suites" element={<Suites />} />
        <Route path="checks" element={<Checks />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  );
}
