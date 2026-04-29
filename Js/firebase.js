// Earnova Firebase Configuration + Django API Shim
// Firebase Auth handles login; the shim redirects Firestore calls to Django.

const API_BASE_URL = 'https://earnova-kz37.onrender.com/api';

const firebaseConfig = {
  apiKey: "AIzaSyB0sKd2AquQRMCEr3i3Cr7D1vCDbZRcSXM",
  authDomain: "earnova-1ff64.firebaseapp.com",
  databaseURL: "https://earnova-1ff64-default-rtdb.firebaseio.com",
  projectId: "earnova-1ff64",
  storageBucket: "earnova-1ff64.firebasestorage.app",
  messagingSenderId: "532356848803",
  appId: "1:532356848803:web:f94528d77f42a7310cf316",
  measurementId: "G-L8G1NFDQ5B"
};

if (typeof firebase !== 'undefined') {
  firebase.initializeApp(firebaseConfig);
  console.log('Firebase Auth initialized successfully');
} else {
  console.warn('Firebase SDK not loaded');
}
window.firebaseApp = firebase;

// ============================================================================
// FIRESTORE SHIM (Django Adapter)
// ============================================================================

class FirestoreShim {
  constructor() {
    this.FieldValue = {
      serverTimestamp: () => ({ _isFieldValue: true, _method: 'serverTimestamp' }),
      increment: (val) => ({ _isFieldValue: true, _method: 'increment', _value: val }),
      delete: () => ({ _isFieldValue: true, _method: 'delete' })
    };
  }

  async _fetch(endpoint, method = 'GET', data = null) {
    const user = firebase.auth().currentUser;
    const headers = { 'Content-Type': 'application/json' };
    if (user) {
      const token = await user.getIdToken();
      headers['Authorization'] = `Bearer ${token}`;
    }
    const options = { method, headers };
    if (data) options.body = JSON.stringify(data);
    const response = await fetch(`${API_BASE_URL}${endpoint}`, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `API Error: ${response.status}`);
    return result;
  }

  collection(path) {
    return new CollectionRef(this, path);
  }
}

class CollectionRef {
  constructor(db, path) {
    this.db = db;
    this.path = path;
  }

  doc(id) {
    return new DocRef(this.db, `${this.path}/${id}`);
  }

  async get() {
    return await new Query(this.db, this.path).get();
  }

  async add(data) {
    const parts = this.path.split('/');
    if (parts.length === 3 && parts[0] === 'users' && parts[2] === 'transactions') {
      const uid = parts[1];
      return await this.db._fetch(`/users/${uid}/transactions/`, 'POST', data);
    } else if (parts[0] === 'withdrawals') {
      return await this.db._fetch(`/withdrawals/`, 'POST', data);
    }
    throw new Error(`Collection add not supported for path: ${this.path}`);
  }

  where(field, op, value) {
    return new Query(this.db, this.path).where(field, op, value);
  }

  orderBy(field, dir = 'asc') {
    return new Query(this.db, this.path).orderBy(field, dir);
  }

  limit(n) {
    return new Query(this.db, this.path).limit(n);
  }

  onSnapshot(callback, errorCallback) {
    return new Query(this.db, this.path).onSnapshot(callback, errorCallback);
  }
}

class Query {
  constructor(db, path) {
    this.db = db;
    this.path = path;
    this.filters = [];
    this.orders = [];
    this._limit = 1000;
  }

  where(field, op, value) {
    this.filters.push({ field, op, value });
    return this;
  }

  orderBy(field, dir = 'asc') {
    this.orders.push({ field, dir });
    return this;
  }

  limit(n) {
    this._limit = n;
    return this;
  }

  async get() {
    const parts = this.path.split('/');

    // users/{uid}/transactions sub-collection
    if (parts.length === 3 && parts[0] === 'users' && parts[2] === 'transactions') {
      const uid = parts[1];
      try {
        const results = await this.db._fetch(`/users/${uid}/transactions/?limit=${this._limit}`);
        return this._wrapDocs(results);
      } catch (e) {
        return this._wrapDocs([]);
      }
    }

    // users collection
    if (this.path === 'users') {
      // Check for referral code query
      const refFilter = this.filters.find(f => f.field === 'referralCode');
      if (refFilter) {
        try {
          const r = await this.db._fetch(`/users/by-referral/check/?code=${refFilter.value}`);
          return this._wrapDocs([r]);
        } catch (e) {
          return this._wrapDocs([]);
        }
      }
      // General list
      try {
        const results = await this.db._fetch(`/admin/users/?limit=${this._limit}`);
        return this._wrapDocs(results);
      } catch (e) {
        return this._wrapDocs([]);
      }
    }

    // withdrawals collection
    if (this.path === 'withdrawals') {
      try {
        const results = await this.db._fetch(`/admin/withdrawals/?limit=${this._limit}`);
        return this._wrapDocs(results);
      } catch (e) {
        return this._wrapDocs([]);
      }
    }

    // kyc collection (not fully implemented in backend yet, but shim it)
    if (this.path === 'kyc') {
       return this._wrapDocs([]);
    }

    return this._wrapDocs([]);
  }

  _wrapDocs(rawDocs) {
    const list = Array.isArray(rawDocs) ? rawDocs : (rawDocs.results || []);
    const docs = list.map(d => ({
      id: d.uid || d.doc_id || d.id || 'unknown',
      data: () => d,
      exists: true,
      ref: { id: d.uid || d.doc_id || d.id || 'unknown' }
    }));
    return {
      docs,
      empty: docs.length === 0,
      size: docs.length,
      forEach: (fn) => docs.forEach(fn)
    };
  }

  onSnapshot(callback, errorCallback) {
    let cancelled = false;
    const poll = async () => {
      if (cancelled) return;
      try {
        const snapshot = await this.get();
        if (!cancelled) callback(snapshot);
      } catch (e) {
        if (!cancelled && errorCallback) errorCallback(e);
      }
      if (!cancelled) setTimeout(poll, 10000); // 10s poll
    };
    poll();
    return () => { cancelled = true; };
  }
}

class DocRef {
  constructor(db, path) {
    this.db = db;
    this.path = path;
    const parts = path.split('/');
    this.uid = parts[0] === 'users' ? parts[1] : null;
  }

  collection(subPath) {
    return new CollectionRef(this.db, `${this.path}/${subPath}`);
  }

  async get() {
    const parts = this.path.split('/');

    if (parts[0] === 'admins') {
      try {
        const r = await this.db._fetch(`/admin/verify/`, 'POST');
        return r.is_admin ? { exists: true, data: () => r } : { exists: false, data: () => null };
      } catch (e) {
        return { exists: false, data: () => null };
      }
    }

    if (this.uid) {
      try {
        const data = await this.db._fetch(`/users/${this.uid}/`);
        return { exists: true, data: () => data };
      } catch (e) {
        return { exists: false, data: () => null };
      }
    }
    
    // Other documents?
    return { exists: false, data: () => null };
  }

  async set(data) {
    if (this.uid) {
      return await this.db._fetch(`/users/${this.uid}/`, 'POST', data);
    }
    throw new Error(`Set not supported for path: ${this.path}`);
  }

  async update(data) {
    if (this.uid) {
      return await this.db._fetch(`/users/${this.uid}/`, 'PATCH', data);
    }
    const parts = this.path.split('/');
    if (parts[0] === 'withdrawals') {
      const action = data.status === 'completed' ? 'approve' : 'reject';
      return await this.db._fetch(`/admin/withdrawals/${parts[1]}/`, 'PATCH', { action });
    }
    throw new Error(`Update not supported for path: ${this.path}`);
  }

  onSnapshot(callback, errorCallback) {
    let cancelled = false;
    const poll = async () => {
      if (cancelled) return;
      try {
        const doc = await this.get();
        if (!cancelled) callback(doc);
      } catch (e) {
        if (!cancelled && errorCallback) errorCallback(e);
      }
      if (!cancelled) setTimeout(poll, 8000);
    };
    poll();
    return () => { cancelled = true; };
  }
}

// Override firebase.firestore
const shim = new FirestoreShim();
firebase.firestore = () => shim;
firebase.firestore.FieldValue = shim.FieldValue;

window.createFirstAdmin = async function(email, password) {
  try {
    const uc = await firebase.auth().createUserWithEmailAndPassword(email, password);
    console.log('User created in Auth:', uc.user.uid);
    console.warn('To make admin: use Django admin panel at /admin/');
    return uc.user;
  } catch (e) { console.error(e); }
};
