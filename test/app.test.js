import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {spawn,spawnSync} from 'node:child_process';
import {distance,validateLocation,hash,verify} from '../lib.js';
test('GPS radius, accuracy and password checks',()=>{
 assert.equal(distance(-6,106,-6,106),0);
 assert.ok(distance(0,0,0,1)>111000);
 const s={lat:-6,lng:106,radius:100,max_accuracy:75};
 assert.equal(validateLocation({lat:-6,lng:106,accuracy:10},s),0);
 assert.throws(()=>validateLocation({lat:-7,lng:106,accuracy:10},s),/luar/);
 assert.throws(()=>validateLocation({lat:-6,lng:106,accuracy:90},s),/akurat/);
 assert.throws(()=>validateLocation({lat:'-6',lng:106,accuracy:10},s),/valid/);
 const p=hash('test-password-123');assert.ok(verify('test-password-123',p));assert.ok(!verify('wrong',p));
});
test('complete employee and PJ workflow, access controls, and persistence',async()=>{
 const dir=mkdtempSync(path.join(tmpdir(),'ruang-test-'));
 const env={...process.env,DATA_DIR:dir,PORT:'3197',HOST:'127.0.0.1',NODE_ENV:'test',ADMIN_EMAIL:'admin@test.local',ADMIN_PASSWORD:'test-admin-password'};
 const init=spawnSync(process.execPath,['server.js','--init'],{env,encoding:'utf8'});assert.equal(init.status,0,init.stderr);
 let child;
 async function start(){child=spawn(process.execPath,['server.js'],{env,stdio:['ignore','pipe','pipe']});await new Promise((resolve,reject)=>{child.stdout.once('data',resolve);child.once('error',reject);child.once('exit',c=>reject(Error('Server exited '+c)));});}
 async function stop(){await new Promise(resolve=>{child.once('exit',resolve);child.kill();});}
 const base='http://127.0.0.1:3197';
 async function call(url,method='GET',data,session={}){const r=await fetch(base+'/api'+url,{method,headers:{cookie:session.cookie||'','x-csrf-token':session.csrf||'','content-type':'application/json'},...(data?{body:JSON.stringify(data)}:{})});const b=await r.json();return {status:r.status,body:b,cookie:r.headers.get('set-cookie')?.split(';')[0]};}
 async function login(email,password){const result=await call('/login','POST',{email,password});assert.equal(result.status,200);const session={cookie:result.cookie};session.csrf=(await call('/me','GET',null,session)).body.csrf;return session;}
 try{
 await start();assert.equal((await fetch(base)).status,200);assert.equal((await call('/attendance')).status,401);
 const admin=await login(env.ADMIN_EMAIL,env.ADMIN_PASSWORD);
 assert.equal((await call('/settings','PUT',{}, {cookie:admin.cookie})).status,403);
 assert.equal((await call('/settings','PUT',{name:'Test Coffee',lat:-6,lng:106,radius:100,max_accuracy:75,timezone:'Asia/Jakarta'},admin)).status,200);
 assert.equal((await call('/users','POST',{name:'Barista Test',email:'staff@test.local',password:'employee-password',role:'employee'},admin)).status,201);
 assert.equal((await call('/users','POST',{name:'Other Staff',email:'other@test.local',password:'employee-password',role:'employee'},admin)).status,201);
 const staff=await login('staff@test.local','employee-password'),other=await login('other@test.local','employee-password');
 assert.equal((await call('/users','GET',null,staff)).status,403);
 const jpeg=Buffer.alloc(1200);jpeg[0]=255;jpeg[1]=216;jpeg[1198]=255;jpeg[1199]=217;
 const event={kind:'in',lat:-6,lng:106,accuracy:12,photo:'data:image/jpeg;base64,'+jpeg.toString('base64')};
 assert.equal((await call('/attendance','POST',{...event,kind:'out'},staff)).status,400);
 assert.equal((await call('/attendance','POST',{...event,lat:-7},staff)).status,400);
 assert.equal((await call('/attendance','POST',{...event,accuracy:200},staff)).status,400);
 assert.equal((await call('/attendance','POST',{...event,photo:''},staff)).status,400);
 assert.equal((await call('/attendance','POST',event,staff)).status,201);
 assert.equal((await call('/attendance','POST',event,staff)).status,400);
 let rows=(await call('/attendance','GET',null,staff)).body;assert.equal(rows.length,1);assert.equal(rows[0].status,'pending');const id=rows[0].id;
 assert.equal((await call('/attendance','GET',null,other)).body.length,0);
 assert.equal((await fetch(base+`/api/attendance/${id}/photo`,{headers:{cookie:other.cookie}})).status,403);
 assert.equal((await fetch(base+`/api/attendance/${id}/photo`,{headers:{cookie:admin.cookie}})).status,200);
 assert.equal((await call(`/attendance/${id}/review`,'POST',{status:'approved'},staff)).status,403);
 assert.equal((await call(`/attendance/${id}/review`,'POST',{status:'flagged',note:'Foto kurang jelas, mohon klarifikasi.'},admin)).status,200);
 assert.equal((await call(`/attendance/${id}/clarify`,'POST',{note:'Kondisi pencahayaan kafe sedang redup.'},staff)).status,200);
 assert.equal((await call(`/attendance/${id}/review`,'POST',{status:'approved',note:'Sudah dikonfirmasi.'},admin)).status,200);
 assert.equal((await call('/attendance','POST',{...event,kind:'out'},staff)).status,201);
 assert.equal((await call('/attendance','POST',{...event,kind:'out'},staff)).status,400);
 await stop();await start();rows=(await call('/attendance','GET',null,staff)).body;assert.equal(rows.length,2);assert.equal(rows.find(r=>r.id===id).status,'approved');
 assert.equal((await call('/password','POST',{current:'employee-password',password:'new-employee-password'},staff)).status,200);
 await login('staff@test.local','new-employee-password');
 const users=(await call('/users','GET',null,admin)).body;const user=users.find(u=>u.email==='staff@test.local');
 assert.equal((await call('/users/'+user.id,'PATCH',{active:false},admin)).status,200);
 assert.equal((await call('/me','GET',null,staff)).status,401);
 assert.equal((await call('/logout','POST',null,admin)).status,200);
 assert.equal((await call('/me','GET',null,admin)).status,401);
 }finally{if(child&&child.exitCode===null)await stop();rmSync(dir,{recursive:true,force:true});}
});

