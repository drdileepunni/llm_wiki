import { createContext, useContext, useState, useEffect } from 'react'
import { listKBs } from './api'

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

  const [activeKB, setActiveKB] = useState('default')
  const [kbList, setKbList] = useState(['default'])

  useEffect(() => {
    listKBs()
      .then(data => {
        const kbs = data.kbs || ['default']
        setKbList(kbs)
        if (kbs.length > 0 && !kbs.includes('default')) {
          setActiveKB(kbs[0])
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
    }}>
      {children}
    </Ctx.Provider>
  )
}

export const useAppState = () => useContext(Ctx)
