import type { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Shell from "./components/Shell";
import { ErrorBox, Loading } from "./components/ui";
import { SessionProvider, useSession } from "./lib/session";
import Keys from "./pages/Keys";
import Login from "./pages/Login";
import BulkImport from "./pages/BulkImport";
import Checks from "./pages/Checks";
import Compare from "./pages/Compare";
import CostDashboard from "./pages/CostDashboard";
import NewRun from "./pages/NewRun";
import RunDetail from "./pages/RunDetail";
import Runs from "./pages/Runs";
import Scenarios from "./pages/Scenarios";
import ScenarioEditor from "./pages/ScenarioEditor";
import ScenarioForm from "./pages/ScenarioForm";
import Settings from "./pages/Settings";
import Simulator from "./pages/Simulator";
import Suites from "./pages/Suites";
import TokensDashboard from "./pages/TokensDashboard";
import Trend from "./pages/Trend";
import Usage from "./pages/Usage";

/** Nothing loads until the server has said who the console talks as (ADR-0050). In accounts
 * mode, someone not signed in is sent to sign in; shared-token mode is as it was. */
function Gate({ children }: { children: ReactNode }) {
  const { session, error } = useSession();
  if (error) return <div className="p-10"><ErrorBox error={error} /></div>;
  if (!session) return <Loading />;
  if (session.mode === "accounts" && !session.user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <SessionProvider>
      <AppRoutes />
    </SessionProvider>
  );
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="login" element={<Login />} />
      <Route element={<Gate><Shell /></Gate>}>
        <Route index element={<Runs />} />
        <Route path="runs/new" element={<NewRun />} />
        <Route path="runs/:id" element={<RunDetail />} />
        <Route path="compare" element={<Compare />} />
        <Route path="trend" element={<Trend />} />
        <Route path="usage" element={<Usage />} />
        <Route path="cost" element={<CostDashboard />} />
        <Route path="tokens" element={<TokensDashboard />} />
        <Route path="simulator" element={<Simulator />} />
        <Route path="scenarios" element={<Scenarios />} />
        <Route path="scenarios/new" element={<ScenarioForm />} />
        <Route path="scenarios/new/yaml" element={<ScenarioEditor />} />
        <Route path="scenarios/edit" element={<ScenarioEditor />} />
        <Route path="import" element={<BulkImport />} />
        <Route path="suites" element={<Suites />} />
        <Route path="checks" element={<Checks />} />
        <Route path="settings" element={<Settings />} />
        <Route path="keys" element={<Keys />} />
      </Route>
    </Routes>
  );
}
