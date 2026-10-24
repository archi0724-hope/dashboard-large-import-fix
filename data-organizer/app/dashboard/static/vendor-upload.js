"use strict";
const $=id=>document.getElementById(id),settings=new URLSearchParams(location.search);
const SESSION='vendor_upload_session',JOB='vendor_upload_job';
let token=location.hash.slice(1)||sessionStorage.getItem(SESSION)||'',busy=false,available=null;
if(token)sessionStorage.setItem(SESSION,token);
history.replaceState(null,'',location.pathname+location.search);
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const show=(text,error=false)=>{$('message').textContent=text;$('message').className=error?'error':'';};
function savedJob(){try{return JSON.parse(sessionStorage.getItem(JOB)||'null');}catch{sessionStorage.removeItem(JOB);return null;}}
function selectSource(){const local=$('computer').checked;$('computerPane').hidden=!local;$('drivePane').hidden=local;}
$('drive').onchange=selectSource;$('computer').onchange=selectSource;
if(settings.get('source')==='computer'||savedJob())$('computer').checked=true;
selectSource();
$('start').disabled=true;
window.addEventListener('beforeunload',event=>{if(busy){event.preventDefault();event.returnValue='';}});

async function api(path,{method='GET',retry=true}={}){
 for(let attempt=0;attempt<(retry?5:1);attempt++){
  try{
   const headers={'X-Upload-Token':token},master=sessionStorage.getItem('organizer_token');
   if(master)headers['X-Token']=master;
   const response=await fetch('/api/vendor-uploads/'+path,{method,headers,cache:'no-store',signal:AbortSignal.timeout(20000)});
   const text=await response.text();let data;
   try{data=text?JSON.parse(text):null;}catch{data=null;}
   if(response.status===401||response.status===404){const error=Error(data?.detail||(response.status===401?'Your upload session expired. Open the upload page again from the dashboard.':'The upload is no longer on this server. Already saved documents remain; select the ZIP again to skip duplicates.'));error.fatal=true;throw error;}
   if(![429,502,503,504].includes(response.status)&&!response.ok){const error=Error(typeof data?.detail==='string'?data.detail:'The server could not accept this request.');error.fatal=true;throw error;}
   const valid=data&&!Array.isArray(data)&&(path==='capacity'?Number.isFinite(data.available_temporary_bytes):path==='session'?typeof data.token==='string':typeof data.status==='string');
   if(!response.ok||!valid)throw Error('Temporary connection interruption.');
   return data;
  }catch(error){if(error.fatal)throw error;if(attempt<4&&retry){show('Connection interrupted. Reconnecting to upload progress…');await sleep(Math.min(8000,1000*2**attempt));}}
 }
 throw Error('Could not reconnect. Click Resume progress to try again. Saved documents are kept.');
}

async function watch(job){
 $('resume').hidden=true;$('progress').removeAttribute('value');
 for(;;){
  const state=await api(job.id);
  if(state.status==='failed'){sessionStorage.removeItem(JOB);const error=Error(state.error||'Import stopped. Saved documents are kept.');error.fatal=true;throw error;}
  if(state.status==='done'){
   const result=state.result;$('progress').value=100;sessionStorage.removeItem(JOB);
   const expected=Number(settings.get('expected')||0),warning=expected&&expected!==result.detected_companies?`\nExpected ${expected} companies; this ZIP identified ${result.detected_companies}. Review the company list.`:'';
   show(`Saved ${result.saved} documents. Skipped ${result.duplicates} duplicates. ${result.issues} files need attention. Total companies: ${result.companies}.`+warning+'\nReturn to the dashboard to view saved documents.');return;
  }
  show(`Saving documents: ${state.processed||0} processed`+(state.current_file?` · ${state.current_file}`:'')+'.');
  await sleep(2000);
 }
}

async function track(job){busy=true;$('start').disabled=true;$('file').disabled=true;
 try{await watch(job);}catch(error){show(error.message,true);if(error.fatal)sessionStorage.removeItem(JOB);$('resume').hidden=!savedJob();}
 finally{busy=false;$('start').disabled=false;$('file').disabled=false;}}
$('resume').onclick=()=>{const job=savedJob();if(job)track(job);};

$('start').onclick=async()=>{
 const file=$('file').files[0];
 if(!file||!file.name.toLowerCase().endsWith('.zip'))return show('Choose a ZIP using the file picker.',true);
 if(savedJob())return show('Resume the previous import before starting another upload.',true);
 busy=true;$('start').disabled=true;$('file').disabled=true;
 let job=null;
 try{
  const capacity=await api('capacity');available=capacity.available_temporary_bytes;
  if(file.size>available)throw Error('This ZIP exceeds available temporary server space. Select Google Drive above and paste its folder or ZIP link instead.');
  job={id:crypto.randomUUID().replaceAll('-',''),name:file.name,size:file.size};sessionStorage.setItem(JOB,JSON.stringify(job));
  $('progress').value=0;show('Uploading ZIP…');
  await new Promise((resolve,reject)=>{
   const query=new URLSearchParams({filename:file.name,company:settings.get('company')||'',read_pdf:settings.get('read_pdf')||'false',upload_id:job.id});
   const xhr=new XMLHttpRequest();xhr.open('POST','/api/vendor-uploads?'+query);xhr.setRequestHeader('X-Upload-Token',token);xhr.setRequestHeader('Content-Type','application/zip');
   const began=performance.now();
   xhr.upload.onprogress=event=>{const percent=event.lengthComputable?event.loaded/event.total*100:0;$('progress').value=percent;const mb=event.loaded/1024**2,seconds=(performance.now()-began)/1000;show(`Uploading: ${mb.toFixed(1)} / ${(file.size/1024**2).toFixed(1)} MB (${percent.toFixed(0)}%)`+(seconds>0?` · ${(mb/seconds).toFixed(1)} MB/s`:''));};
   xhr.upload.onload=()=>show('Upload sent. Starting import…');
   xhr.onerror=()=>reject(Error('The upload connection was interrupted. Checking whether the server accepted it…'));
   xhr.onload=()=>{let data;try{data=JSON.parse(xhr.responseText);}catch{reject(Error('The server returned an empty or interrupted response. Reconnecting to import progress…'));return;}if(xhr.status>=200&&xhr.status<300&&data?.job_id===job.id)resolve();else{const error=Error(data?.detail||'Upload failed. Retry or use a Drive link.');error.fatal=![429,502,503,504].includes(xhr.status);reject(error);}};
   xhr.send(file);
  });
 }catch(error){
  if(!job||error.fatal){if(job)sessionStorage.removeItem(JOB);show(error.message,true);busy=false;$('start').disabled=false;$('file').disabled=false;return;}
  show(error.message);
 }
 if(job){await track(job);}else{busy=false;$('start').disabled=false;$('file').disabled=false;}
};

(async()=>{try{
 const expiry=Number(token.split(':')[0])*1000;
 if(!token||!Number.isFinite(expiry)||expiry<=Date.now()){const session=await api('session',{method:'POST',retry:false});token=session.token;sessionStorage.setItem(SESSION,token);}
 $('driveFrame').src='/drive-library#'+token;
 const capacity=await api('capacity');available=capacity.available_temporary_bytes;$('capacity').textContent=`Currently available temporary space: ${(available/1024**3).toFixed(1)} GiB. This is working space, not permanent storage.`;
 $('start').disabled=false;
 const job=savedJob();if(job)await track(job);
}catch(error){$('authError').textContent=error.message;$('start').disabled=true;}})();
