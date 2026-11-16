import {DefaultArtifactClient} from '@actions/artifact';
import {spawn} from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';

const repo=process.env.GITHUB_REPOSITORY;
if(repo!=='alihbitis-gif/-boru-paribu-simulasyon-') throw new Error('Unexpected repository');
const token=process.env.GITHUB_TOKEN;
const pr=Number(process.env['INPUT_PR-NUMBER']);
if(pr!==1) throw new Error('Unexpected notification pull request');
const mode=process.env.INPUT_MODE;
const artifact=new DefaultArtifactClient();
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const out=path.resolve('data/trade_reports');
let announced=0;
const ownSnapshots=[];
async function api(endpoint,method='GET',body){
  const r=await fetch(`https://api.github.com/repos/${repo}/${endpoint}`,{
    method,headers:{Authorization:`Bearer ${token}`,Accept:'application/vnd.github+json','Content-Type':'application/json'},
    ...(body?{body:JSON.stringify(body)}:{})});
  if(!r.ok) throw new Error(`GitHub ${r.status}: ${endpoint}`);
  return r.json();
}
async function publish({readiness=false}={}){
  const index=JSON.parse(await fs.readFile(path.join(out,'index.json'),'utf8'));
  if(!readiness && index.trades.length<=announced) return;
  const name=`boru-graphics-${process.env.GITHUB_RUN_ID}-${index.session_id}-${index.trades.length}-${Date.now()}`;
  const files=(await fs.readdir(out)).filter(n=>/\.(png|md|json)$/.test(n)).map(n=>path.join(out,n));
  const result=await artifact.uploadArtifact(name,files,out,{retentionDays:30});
  if(!result.id) throw new Error('Artifact upload returned no ID');
  console.log(`BORU_GRAPHICS_UPLOADED artifact_id=${result.id} reports=${index.trades.length}`);
  if(index.trades.length>announced){
    const marker=`BORU_REPORT_READY:${index.session_id}:${index.trades.length}`;
    // Neutral routing metadata only. Report contents stay in authenticated artifacts.
    const body=`<!-- ${marker} -->\nBÖRÜ sanal işlem grafik raporları hazır.\n\nRun: ${process.env.GITHUB_RUN_ID}\nArtifact: ${result.id}\nSession: ${index.session_id}\nRapor sayısı: ${index.trades.length}\n\n[Grafikli rapor paketi](https://github.com/${repo}/actions/runs/${process.env.GITHUB_RUN_ID}/artifacts/${result.id})`;
    const previous=await api(`issues/${pr}/comments?per_page=100`);
    if(!previous.some(c=>c.body?.includes(`<!-- ${marker} -->`))) await api(`issues/${pr}/comments`,'POST',{body});
    announced=index.trades.length;
  }
  ownSnapshots.push(name);
  // Each new snapshot includes every earlier report; retain the latest two.
  while(ownSnapshots.length>2){
    const old=ownSnapshots.shift();
    await artifact.deleteArtifact(old);
  }
}
function python(args,notify=false){
  return new Promise((resolve,reject)=>{
    const child=spawn('python',['-u',...args],{env:{...process.env,BORU_GRAPH_REPORTS:'1'},stdio:['ignore','pipe','inherit']});
    const lines=readline.createInterface({input:child.stdout});
    let queue=Promise.resolve();
    lines.on('line',line=>{
      console.log(line);
      if(notify && line.startsWith('BORU_REPORTS_READY ')){
        queue=queue.then(async()=>{
          for(let attempt=0;attempt<3;attempt++){
            try{await publish();return;}catch(e){console.warn(`Report delivery attempt ${attempt+1}: ${e.message}`);await wait(2000*(attempt+1));}
          }
          throw new Error('Chart delivery failed after retries');
        }).catch(e=>{console.error(e.message);process.exitCode=1;});
      }
    });
    child.on('error',reject);
    child.on('exit',code=>queue.then(()=>code===0?resolve():reject(new Error(`Python exited ${code}`))));
  });
}
async function downloadSource(runId,name){
  const entries=(await api(`actions/runs/${runId}/artifacts?per_page=100`)).artifacts;
  const item=entries.find(a=>a.name===name && !a.expired);
  if(!item) throw new Error(`Source artifact missing: ${name}`);
  const [repositoryOwner,repositoryName]=repo.split('/');
  await artifact.downloadArtifact(item.id,{path:path.resolve('data'),findBy:{token,workflowRunId:runId,repositoryOwner,repositoryName}});
}
try{
  if(mode==='collect'){
    const source=Number(process.env['INPUT_SOURCE-RUN']);
    if(source!==43849993630) throw new Error('Unexpected source run');
    await downloadSource(source,'boru-initial-state');
    await python(['reporting.py','--directory','data']);
    await publish({readiness:true});
    const deadline=Date.now()+195*60*1000;
    let state;
    while(Date.now()<deadline){
      state=await api(`actions/runs/${source}`);
      if(state.status==='completed') break;
      console.log('Waiting for original simulation; original run remains unchanged.');
      await wait(30000);
    }
    if(state?.status!=='completed') throw new Error('Original simulation did not finish within collection window');
    await downloadSource(source,'boru-session-results');
    await python(['reporting.py','--directory','data']);
    await publish({readiness:true});
  }else if(mode==='simulate'){
    await python(['boru.py','--directory','data','--duration-minutes','180','--interval','15','--require-data'],true);
    await python(['reporting.py','--directory','data']);
    await publish({readiness:true});
  }else throw new Error('Unsupported mode');
}catch(e){console.error(e.message);process.exitCode=1;}
