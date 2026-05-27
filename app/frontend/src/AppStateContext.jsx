import { createContext, useContext, useState, useEffect } from 'react'
import { listKBs, listStudies } from './api'

const Ctx = createContext(null)

export function AppStateProvider({ children }) {
  const [chat, setChat] = useState({ messages: [], input: '' })

  const [ingest, setIngest] = useState({
    tab: 'file', pmid: '', file: null, result: null, error: null,
  })

  const [wiki, setWiki] = useState({
    tree: [],
    selectedPath: null,
    content: '',
    savedContent: '',
    mode: 'preview',
    searchQuery: '',
    searchResults: null,
    searchTotal: 0,
  })

  const [activeKB, setActiveKB] = useState(null)
  const [kbList, setKbList] = useState([])

  // Shared study state — used by Study page and FN Review page
  const [studies, setStudies]         = useState([])
  const [activeStudy, setActiveStudy] = useState(null)

  useEffect(() => {
    listKBs()
      .then(data => {
        const kbs = data.kbs || []
        setKbList(kbs)
        if (kbs.length > 0) {
          setActiveKB(prev => prev && kbs.includes(prev) ? prev : kbs[0])
        }
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    listStudies()
      .then(data => {
        const list = data.studies || []
        setStudies(list)
        if (list.length > 0) {
          setActiveStudy(prev => prev ? (list.find(s => s.study_id === prev.study_id) || list[0]) : (list.find(s => s.status === 'active') || list[0]))
        }
      })
      .catch(() => {})
  }, [])

  // Reset wiki state when switching KBs so the tree reloads
  function switchKB(name) {
    setActiveKB(name)
    setWiki(w => ({ ...w, tree: [], selectedPath: null, content: '', savedContent: '' }))
  }

  return (
    <Ctx.Provider value={{
      chat, setChat,
      ingest, setIngest,
      wiki, setWiki,
      activeKB, switchKB,
      kbList, setKbList,
      studies, setStudies,
      activeStudy, setActiveStudy,
    }}>
      {children}
    </Ctx.Provider>
  )
}

export const useAppState = () => useContext(Ctx)
