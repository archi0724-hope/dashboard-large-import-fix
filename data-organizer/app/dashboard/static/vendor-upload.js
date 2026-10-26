"use strict";
const $=id=>document.getElementById(id),settings=new URLSearchParams(location.search);
const SESSION='vendor_upload_session',JOB='vendor_upload_job';
function savedJob(){try{return JSON.parse(sessionStorage.getItem(JOB)||'null');}catch{sessionStorage.removeItem(JOB);return null;}}
// Keep the scope owning an unfinished transfer when a dashboard supplies a fresh token.
let token=(savedJob()&&sessionStorage.getItem(SESSION))||location.hash.slice(1)||sessionStorage.getItem(SESSION)||'';
let busy=false,renewing=null;
if(token)sessionStorage.setItem(SESSION,token);
history.replaceState(null,'',location.pathname+location.search);
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const show=(text,error=false)=>{$('message').textContent=text;$('message').className=error?'error':'';};
function selectSource(){const local=$('computer').checked;$('computerPane').hidden=!local;$('drivePane').hidden=local;}
$('drive').onchange=selectSource;$('computer').onchange=selectSource;
if(settings.get('source')==='computer'||savedJob())$('computer').checked=true;
selectSource();$('start').disabled=true;
window.addEventListener('beforeunload',event=>{if(busy){event.preventDefault();event.returnValue='';}});

async function renewSession(){
 if(renewing)return renewing;
 renewing=(async()=>{
  const headers={'X-Upload-Token':token},master=sessionStorage.getItem('organizer_token');
  if(master)headers['X-Token']=master;
  const response=await fetch('/api/vendor-uploads/session',{method:'POST',headers,signal:AbortSignal.timeout(20000)});
  let data;try{data=JSON.parse(await response.text());}catch{data=null;}
  if(!response.ok||!data?.token)throw Error('Return to the dashboard and reopen Add vendor documents to renew your upload session.');
  token=data.token;sessionStorage.setItem(SESSION,token);
 })();
 try{await renewing;}finally{renewing=null;}
}

async function api(path,{method='GET',body,retry=true}={}){
 const attempts=retry?5:1;
 for(let attempt=0;attempt<attempts;attempt++){
  try{
   if(Number(token.split(':')[0])*1000<Date.now()+60000)await renewSession();
   const headers={'X-Upload-Token':token};if(body)headers['Content-Type']='application/octet-stream';
   let response=await fetch('/api/vendor-uploads/'+path,{method,body,headers,cache:'no-store',signal:AbortSignal.timeout(body?120000:20000)});
   if(response.status===401){await renewSession();headers['X-Upload-Token']=token;response=await fetch('/api/vendor-uploads/'+path,{method,body,headers,cache:'no-store',signal:AbortSignal.timeout(body?120000:20000)});}
   let data;try{data=JSON.parse(await response.text());}catch{data=null;}
   if(response.status===409){const error=Error(data?.detail||'Upload progress changed.');error.conflict=true;throw error;}
   if(!response.ok&&![429,500,502,503,504].includes(response.status)){
    const message=response.status===404?'This transfer is no longer on the server. Select the ZIP and upload again. Already saved documents are kept.':data?.detail;
    const error=Error(typeof message==='string'?message:'The server could not accept this request.');error.fatal=true;error.missing=response.status===404;throw error;
   }
   if(!response.ok||!data||Array.isArray(data))throw Error('Temporary connection interruption.');
   return data;
  }catch(error){
   if(error.fatal||error.conflict)throw error;
   if(attempt===attempts-1)throw Error('Connection interrupted. Click Resume progress to reconnect. Your last confirmed upload chunk is kept while this server is running.');
   show('Connection interrupted. Reconnecting…');await sleep(Math.min(8000,1000*2**attempt));
  }
 }
}

function clearJob(){sessionStorage.removeItem(JOB);$('resume').hidden=true;$('cancel').hidden=true;}
function setBusy(value){busy=value;$('start').disabled=value;$('file').disabled=value;$('cancel').disabled=value;}
function matches(file,job){return file&&file.name===job.name&&file.size===job.size&&(job.modified===undefined||job.modified===file.lastModified);}

async function transfer(job,file,state){
 if(!matches(file,job)){show(`Upload paused at ${((state.received||0)/1024**2).toFixed(1)} MB. Choose the same ZIP (${job.name}) and click Upload and save documents to continue.`);return false;}
 const chunkBytes=state.chunk_bytes;
 if(!Number.isSafeInteger(chunkBytes)||chunkBytes<=0||chunkBytes>8*1024**2)throw Error('Reopen the upload page to use the current uploader.');
 let offset=state.received,failures=0;
 while(offset<file.size){
  const end=Math.min(file.size,offset+chunkBytes);
  show(`Uploading: ${(offset/1024**2).toFixed(1)} / ${(file.size/1024**2).toFixed(1)} MB. Confirmed chunks can be resumed.`);
  $('progress').value=offset/file.size*100;
  try{
   const result=await api(job.id+'/chunk?offset='+offset,{method:'PUT',body:file.slice(offset,end),retry:false});
   if(result.received!==end)throw Error('The server did not confirm this chunk.');
   offset=end;failures=0;
  }catch(error){
   if(error.fatal)throw error;
   // Reconcile a lost acknowledgement before retrying the same bytes.
   state=await api(job.id);
   if(state.status!=='uploading')return true;
   if(!Number.isSafeInteger(state.received)||state.received<offset||state.received>end)throw Error('Upload progress changed unexpectedly. Resume progress before continuing.');
   if(state.received>offset){offset=state.received;failures=0;}
   else if(++failures>=5)throw Error('Transfer paused after repeated connection errors. Click Resume progress to continue from the last confirmed chunk.');
   else{show('Retrying the interrupted upload chunk…');await sleep(Math.min(8000,1000*2**(failures-1)));}
  }
 }
 $('progress').value=100;show('Upload received. Checking ZIP and starting import…');
 await api(job.id+'/complete',{method:'POST'});return true;
}

async function watch(job,file){
 $('resume').hidden=true;
 for(;;){
  const state=await api(job.id);
  if(state.status==='uploading'){
   if(state.received===state.size&&state.chunk_bytes){await api(job.id+'/complete',{method:'POST'});continue;}
   if(!await transfer(job,file,state))return;
   continue;
  }
  if(state.status==='failed'){clearJob();const error=Error(state.error||'Import stopped. Saved documents are kept.');error.fatal=true;throw error;}
  if(state.status==='done'){
   clearJob();$('progress').value=100;
   const result=state.result,expected=Number(settings.get('expected')||0);
   const warning=expected&&expected!==result.detected_companies?`\nExpected ${expected} companies; this ZIP identified ${result.detected_companies}. Review the company list.`:'';
   show(`Saved ${result.saved} documents. Skipped ${result.duplicates} duplicates. ${result.issues} files need attention. Total companies: ${result.companies}.`+warning+'\nReturn to the dashboard to view saved documents.');return;
  }
  if(state.status!=='importing')throw Error('The server returned an unknown upload state.');
  $('progress').removeAttribute('value');show(`Saving documents: ${state.processed||0} processed`+(state.current_file?` · ${state.current_file}`:'')+'.');
  await sleep(2000);
 }
}

async function track(job,file){
 setBusy(true);
 try{await watch(job,file);}catch(error){if(error.missing)clearJob();show(error.message,true);}
 finally{setBusy(false);$('resume').hidden=!savedJob();$('cancel').hidden=!savedJob();}
}
$('resume').onclick=()=>{const job=savedJob();if(job)track(job,$('file').files[0]);};
$('cancel').onclick=async()=>{
 const job=savedJob();if(!job)return;
 setBusy(true);
 try{await api(job.id,{method:'DELETE'});clearJob();show('Transfer cancelled. Choose a ZIP to start again. Saved documents are kept.');}
 catch(error){if(error.missing)clearJob();show(error.message,true);}finally{setBusy(false);}
};
$('start').onclick=async()=>{
 const file=$('file').files[0];
 if(!file||!file.name.toLowerCase().endsWith('.zip'))return show('Choose a ZIP using the file picker.',true);
 const previous=savedJob();
 if(previous){if(!matches(file,previous))return show('Choose the same ZIP to resume, or cancel the unfinished transfer before selecting another ZIP.',true);return track(previous,file);}
 setBusy(true);let job=null;
 try{
  const capacity=await api('capacity');
  if(file.size>capacity.available_temporary_bytes)throw Error('This ZIP exceeds available temporary server space. Select Google Drive above and paste its folder or ZIP link instead.');
  job={id:crypto.randomUUID().replaceAll('-',''),name:file.name,size:file.size,modified:file.lastModified};
  sessionStorage.setItem(JOB,JSON.stringify(job));
  show('Preparing upload…');
  const query=new URLSearchParams({filename:file.name,size:String(file.size),company:settings.get('company')||'',read_pdf:settings.get('read_pdf')||'false',upload_id:job.id});
  await api('begin?'+query,{method:'POST'});
 }catch(error){
  if(job&&error.fatal)clearJob();show(error.message,true);setBusy(false);$('resume').hidden=!savedJob();$('cancel').hidden=!savedJob();return;
 }
 await track(job,file);
};

(async()=>{try{
 await renewSession();
 $('driveFrame').src='/drive-library#'+token;
 const capacity=await api('capacity');
 $('capacity').textContent=`Currently available temporary space: ${(capacity.available_temporary_bytes/1024**3).toFixed(1)} GiB. This is working space, not permanent storage.`;
 $('start').disabled=false;
 const job=savedJob();if(job)await track(job);
}catch(error){$('authError').textContent=error.message;$('start').disabled=true;}})();
