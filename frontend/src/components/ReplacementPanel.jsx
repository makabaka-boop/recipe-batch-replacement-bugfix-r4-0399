import { useState } from 'react'

export function ReplacementPanel({ version, run }) {
  const [roots, setRoots] = useState(String(version.id))
  const [oldBatch, setOldBatch] = useState('')
  const [material, setMaterial] = useState('')
  const [batch, setBatch] = useState('')
  const [name, setName] = useState('审核员')
  const [plan, setPlan] = useState(null)
  const request = async (url, body) => {
    const res = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '请求失败')
    return data
  }
  const preview = async () => {
    const data = await run(() => request('/api/replacements/preview', {
      roots:roots.split(',').map(x=>Number(x.trim())),
      targets:{[oldBatch]:{material_id:Number(material),batch_id:Number(batch)}},created_by:name
    }))
    if (data) setPlan(data)
  }
  return <section className="panel">
    <h2>联动替代单</h2>
    <label>产品版本（逗号分隔）<input value={roots} onChange={e=>setRoots(e.target.value)} /></label>
    <label>原批次编号<input value={oldBatch} onChange={e=>setOldBatch(e.target.value)} /></label>
    <label>替代原料编号<input value={material} onChange={e=>setMaterial(e.target.value)} /></label>
    <label>替代批次编号<input value={batch} onChange={e=>setBatch(e.target.value)} /></label>
    <label>起草人<input value={name} onChange={e=>setName(e.target.value)} /></label>
    <button onClick={preview}>生成预览</button>
    {plan && <><pre>{JSON.stringify(plan,null,2)}</pre><button disabled={plan.status==='applied'} onClick={async()=>{
      const data=await run(()=>request(`/api/replacements/${plan.id}/apply`,{}));if(data)setPlan(data)
    }}>采纳整单</button></>}
  </section>
}
