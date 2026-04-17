import { createContext, useContext, useState } from 'react'

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

  return (
    <Ctx.Provider value={{ chat, setChat, ingest, setIngest, wiki, setWiki }}>
      {children}
    </Ctx.Provider>
  )
}

export const useAppState = () => useContext(Ctx)
