// ─── Firestore Database Layer ────────────────────────────────────────────────
// Schema:
//   users/{uid}
//     displayName: string
//     email: string
//     photoURL: string
//     plan: "starter" | "pro" | "enterprise"
//     createdAt: timestamp
//     lastLoginAt: timestamp
//
//   users/{uid}/aws_connections/{connectionId}
//     name: string
//     src_account: string
//     src_region: string
//     src_role_arn: string
//     tgt_account: string
//     tgt_region: string
//     tgt_role_arn: string
//     status: "active" | "pending" | "error"
//     createdAt: timestamp
//
//   users/{uid}/migrations/{migrationId}
//     connection_id: string
//     name: string
//     status: "pending" | "running" | "completed" | "failed"
//     resources_count: number
//     createdAt: timestamp
//     completedAt: timestamp | null

import { initializeApp, getApps } from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js';
import {
  getFirestore,
  doc,
  setDoc,
  getDoc,
  updateDoc,
  collection,
  addDoc,
  getDocs,
  deleteDoc,
  serverTimestamp,
  query,
  orderBy,
} from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js';
import { getAuth } from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js';

const FIREBASE_CONFIG = {
  apiKey:            "AIzaSyC2s8vy7THhcs9YO5Ro5lwenICXZpzmgD8",
  authDomain:        "availabl-1f709.firebaseapp.com",
  projectId:         "availabl-1f709",
  storageBucket:     "availabl-1f709.firebasestorage.app",
  messagingSenderId: "25354908364",
  appId:             "1:25354908364:web:7932be5f9c684d6862c869",
};

const app = getApps().length ? getApps()[0] : initializeApp(FIREBASE_CONFIG);
const db  = getFirestore(app);
const auth = getAuth(app);

// ─── Internal helpers ────────────────────────────────────────────────────────

function uid() {
  const user = auth.currentUser;
  if (!user) throw new Error('Not authenticated');
  return user.uid;
}

function sanitizeString(val, maxLen = 256) {
  if (typeof val !== 'string') return '';
  return val.trim().slice(0, maxLen).replace(/[<>"']/g, '');
}

function sanitizeArn(val) {
  if (typeof val !== 'string') return '';
  const clean = val.trim().slice(0, 2048);
  if (clean && !/^arn:aws:/.test(clean)) throw new Error('Invalid ARN format');
  return clean;
}

function sanitizeAccountId(val) {
  if (typeof val !== 'string') return '';
  const clean = val.trim().replace(/\D/g, '');
  if (clean && !/^\d{12}$/.test(clean)) throw new Error('AWS Account ID must be 12 digits');
  return clean;
}

const ALLOWED_REGIONS = [
  'us-east-1','us-east-2','us-west-1','us-west-2',
  'eu-west-1','eu-west-2','eu-west-3','eu-central-1','eu-north-1',
  'ap-southeast-1','ap-southeast-2','ap-northeast-1','ap-northeast-2',
  'ap-south-1','sa-east-1','ca-central-1','me-south-1','af-south-1',
];

function sanitizeRegion(val) {
  if (!ALLOWED_REGIONS.includes(val)) throw new Error(`Invalid region: ${val}`);
  return val;
}

// ─── User profile ────────────────────────────────────────────────────────────

export async function upsertUser(user) {
  const ref  = doc(db, 'users', user.uid);

  const data = {
    displayName:  sanitizeString(user.displayName || '', 128),
    email:        sanitizeString(user.email || '', 320),
    photoURL:     sanitizeString(user.photoURL  || '', 512),
    lastLoginAt:  serverTimestamp(),
    plan:         'starter',
    createdAt:    serverTimestamp(),
  };

  await setDoc(ref, data, { merge: true });
  return data;
}

export async function getUser() {
  const ref  = doc(db, 'users', uid());
  const snap = await getDoc(ref);
  return snap.exists() ? { id: snap.id, ...snap.data() } : null;
}

export async function updateUserName(displayName) {
  const clean = sanitizeString(displayName, 128);
  if (!clean) throw new Error('Name cannot be empty');
  await updateDoc(doc(db, 'users', uid()), { displayName: clean });
}

// ─── AWS Connections ─────────────────────────────────────────────────────────

export async function addAwsConnection(conn) {
  const payload = {
    name:         sanitizeString(conn.name || 'Untitled', 128),
    src_account:  sanitizeAccountId(conn.src_account),
    src_region:   sanitizeRegion(conn.src_region),
    src_role_arn: sanitizeArn(conn.src_role_arn),
    tgt_account:  sanitizeAccountId(conn.tgt_account),
    tgt_region:   sanitizeRegion(conn.tgt_region),
    tgt_role_arn: sanitizeArn(conn.tgt_role_arn),
    status:       'pending',
    createdAt:    serverTimestamp(),
  };

  const ref = await addDoc(
    collection(db, 'users', uid(), 'aws_connections'),
    payload,
  );
  return { id: ref.id, ...payload };
}

export async function getAwsConnections() {
  const q    = query(
    collection(db, 'users', uid(), 'aws_connections'),
    orderBy('createdAt', 'desc'),
  );
  const snap = await getDocs(q);
  return snap.docs.map(d => ({ id: d.id, ...d.data() }));
}

export async function deleteAwsConnection(connectionId) {
  await deleteDoc(doc(db, 'users', uid(), 'aws_connections', connectionId));
}

// ─── Migrations ──────────────────────────────────────────────────────────────

export async function addMigration(migration) {
  const payload = {
    connection_id:   sanitizeString(migration.connection_id || '', 128),
    name:            sanitizeString(migration.name || 'Untitled migration', 128),
    status:          'pending',
    resources_count: 0,
    createdAt:       serverTimestamp(),
    completedAt:     null,
  };
  const ref = await addDoc(
    collection(db, 'users', uid(), 'migrations'),
    payload,
  );
  return { id: ref.id, ...payload };
}

export async function getMigrations() {
  const q    = query(
    collection(db, 'users', uid(), 'migrations'),
    orderBy('createdAt', 'desc'),
  );
  const snap = await getDocs(q);
  return snap.docs.map(d => ({ id: d.id, ...d.data() }));
}
