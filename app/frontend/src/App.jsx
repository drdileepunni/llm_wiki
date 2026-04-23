import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AppStateProvider } from './AppStateContext'
import Layout from './components/Layout'
import Ingest from './pages/Ingest'
import Chat from './pages/Chat'
import Dashboard from './pages/Dashboard'
import Wiki from './pages/Wiki'
import KBSettings from './pages/KBSettings'
import Assess from './pages/Assess'
import ClinicalAssess from './pages/ClinicalAssess'
import Learn from './pages/Learn'

export default function App() {
  return (
    <AppStateProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Layout />}>
            <Route index element={<Navigate to="/ingest" replace />} />
            <Route path="ingest" element={<Ingest />} />
            <Route path="chat" element={<Chat />} />
            <Route path="wiki" element={<Wiki />} />
            <Route path="dashboard" element={<Dashboard />} />
            <Route path="kb-settings" element={<KBSettings />} />
            <Route path="assess" element={<Assess />} />
            <Route path="clinical-assess" element={<ClinicalAssess />} />
            <Route path="learn" element={<Learn />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </AppStateProvider>
  )
}
