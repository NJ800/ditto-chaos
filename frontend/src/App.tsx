import { Link, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Empty } from "./components/ui";
import { Agents } from "./pages/Agents";
import { Control } from "./pages/Control";
import { Dependencies } from "./pages/Dependencies";
import { ExperimentDetailPage, History } from "./pages/History";
import { Insights } from "./pages/Insights";
import { Monitoring } from "./pages/Monitoring";
import { Overview } from "./pages/Overview";

function NotFound() {
  return (
    <Empty>
      This page does not exist.{" "}
      <Link className="text-accent hover:underline" to="/">
        Go to the overview
      </Link>
    </Empty>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Overview />} />
        <Route path="/monitoring" element={<Monitoring />} />
        <Route path="/control" element={<Control />} />
        <Route path="/experiments" element={<Control />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/history" element={<History />} />
        <Route path="/history/:id" element={<ExperimentDetailPage />} />
        <Route path="/insights" element={<Insights />} />
        <Route path="/dependencies" element={<Dependencies />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
