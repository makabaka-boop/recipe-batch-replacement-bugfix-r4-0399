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
  const p = plan && plan.preview
  const result = plan && plan.result
  return <section className="panel">
    <h2>联动替代单</h2>
    <label>产品版本（逗号分隔）<input value={roots} onChange={e=>setRoots(e.target.value)} /></label>
    <label>原批次编号<input value={oldBatch} onChange={e=>setOldBatch(e.target.value)} /></label>
    <label>替代原料编号<input value={material} onChange={e=>setMaterial(e.target.value)} /></label>
    <label>替代批次编号<input value={batch} onChange={e=>setBatch(e.target.value)} /></label>
    <label>起草人<input value={name} onChange={e=>setName(e.target.value)} /></label>
    <button onClick={preview}>生成预览</button>
    {plan && <>
      {p && <div className="plan-summary">
        <h3>受影响版本（整单 {p.affected.length} 个，同一次替代）</h3>
        <ul>{p.affected.map(a => <li key={a.version_id}>#{a.version_id} {a.recipe}（指纹 {a.content_hash}）</li>)}</ul>
        <h3>批次替换</h3>
        <ul>{p.changes.map((c, i) => <li key={i}>{c.recipe}：{c.old_batch_code} → {c.new_batch_code}</li>)}</ul>
      </div>}
      {result && <div className="plan-summary">
        <h3>采纳结果（旧版本 → 新版本）</h3>
        <ul>{Object.entries(result.versions).map(([oldV, newV]) => <li key={oldV}>#{oldV} → #{newV}</li>)}</ul>
      </div>}
      <pre>{JSON.stringify(plan,null,2)}</pre>
      <button disabled={plan.status==='applied'} onClick={async()=>{
        const data=await run(()=>request(`/api/replacements/${plan.id}/apply`,{}));if(data)setPlan(data)
      }}>采纳整单</button>
    </>}
  </section>
}
