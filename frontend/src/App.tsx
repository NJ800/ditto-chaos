import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Overview } from "./pages/Overview";
import { ExperimentCenter } from "./pages/ExperimentCenter";
import { LiveMonitoring } from "./pages/LiveMonitoring";
import { History } from "./pages/History";
import { ExperimentDetailPage } from "./pages/ExperimentDetail";
import { Insights } from "./pages/Insights";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Overview />} />
        <Route path="/experiments" element={<ExperimentCenter />} />
        <Route path="/monitoring" element={<LiveMonitoring />} />
        <Route path="/history" element={<History />} />
        <Route path="/history/:id" element={<ExperimentDetailPage />} />
        <Route path="/insights" element={<Insights />} />
      </Route>
    </Routes>
  );
}
