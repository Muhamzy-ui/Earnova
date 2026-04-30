// Earnova Firebase Configuration + Django API Shim
// Firebase Auth handles login; the shim redirects Firestore calls to Django.

const API_BASE_URL = 'https://api.earnova.cloud/api';

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
    this._listeners = new Set();
  }

  async _fetch(endpoint, method = 'GET', data = null) {
    const user = firebase.auth().currentUser;
    const headers = { 'Content-Type': 'application/json' };
    
    // Wait for user if token is needed
    if (user) {
      const token = await user.getIdToken();
      headers['Authorization'] = `Bearer ${token}`;
    }
    
    const options = { method, headers };
    if (data) options.body = JSON.stringify(data);
    
    try {
      const response = await fetch(`${API_BASE_URL}${endpoint}`, options);
      const result = await response.json();
      if (!response.ok) {
        console.error(`API Error [${response.status}] for ${endpoint}:`, result);
        throw new Error(result.error || `API Error: ${response.status}`);
      }
      return result;
    } catch (err) {
      console.error(`Fetch error for ${endpoint}:`, err);
      throw err;
    }
  }

  collection(path) {
    return new CollectionRef(this, path);
  }

  // Helper to trigger a refresh of all active listeners
  _triggerRefresh() {
    this._listeners.forEach(fn => fn());
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
    let result;
    // Sub-collection: users/{uid}/transactions
    if (parts.length === 3 && parts[0] === 'users' && parts[2] === 'transactions') {
      result = await this.db._fetch(`/users/${parts[1]}/transactions/`, 'POST', data);
    } else if (parts[0] === 'withdrawals') {
      result = await this.db._fetch(`/withdrawals/`, 'POST', data);
    } else {
      throw new Error(`Collection add not supported for path: ${this.path}`);
    }
    this.db._triggerRefresh();
    return { id: result.id || result.doc_id || 'new_doc' };
  }

  where(field, op, value) { return new Query(this.db, this.path).where(field, op, value); }
  orderBy(field, dir = 'asc') { return new Query(this.db, this.path).orderBy(field, dir); }
  limit(n) { return new Query(this.db, this.path).limit(n); }
  onSnapshot(callback, errorCallback) { return new Query(this.db, this.path).onSnapshot(callback, errorCallback); }
}

class Query {
  constructor(db, path) {
    this.db = db;
    this.path = path;
    this.filters = [];
    this.orders = [];
    this._limit = 1000;
  }

  where(field, op, value) { this.filters.push({ field, op, value }); return this; }
  orderBy(field, dir = 'asc') { this.orders.push({ field, dir }); return this; }
  limit(n) { this._limit = n; return this; }

  async get() {
    const parts = this.path.split('/');

    // Handle users/{uid}/transactions
    if (parts.length === 3 && parts[0] === 'users' && parts[2] === 'transactions') {
      const results = await this.db._fetch(`/users/${parts[1]}/transactions/?limit=${this._limit}`);
      let list = Array.isArray(results) ? results : (results.results || []);
      
      // Apply where() filters client-side (e.g. type === 'referral')
      for (const f of this.filters) {
        list = list.filter(doc => {
          const val = doc[f.field];
          if (f.op === '==') return val == f.value;
          if (f.op === '!=') return val != f.value;
          if (f.op === '>') return val > f.value;
          if (f.op === '<') return val < f.value;
          return true;
        });
      }

      // Apply orderBy client-side
      for (const o of this.orders) {
        list.sort((a, b) => {
          const av = a[o.field], bv = b[o.field];
          if (av == null) return 1;
          if (bv == null) return -1;
          return o.dir === 'desc' ? (bv > av ? 1 : -1) : (av > bv ? 1 : -1);
        });
      }

      // Apply limit
      list = list.slice(0, this._limit);
      return this._wrapDocs(list);
    }

    // Handle users collection
    if (this.path === 'users') {
      const refFilter = this.filters.find(f => f.field === 'referralCode');
      if (refFilter) {
        try {
          const r = await this.db._fetch(`/users/by-referral/check/?code=${refFilter.value}`);
          return this._wrapDocs([r]);
        } catch (e) { return this._wrapDocs([]); }
      }
      const results = await this.db._fetch(`/admin/users/?limit=${this._limit}`);
      return this._wrapDocs(results);
    }

    // Handle withdrawals collection
    if (this.path === 'withdrawals') {
      const results = await this.db._fetch(`/admin/withdrawals/?limit=${this._limit}`);
      return this._wrapDocs(results);
    }

    // Kyc (dummy)
    if (this.path === 'kyc') return this._wrapDocs([]);

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
      } catch (e) { if (!cancelled && errorCallback) errorCallback(e); }
      if (!cancelled) setTimeout(poll, 15000); // Poll less frequently for lists
    };
    this.db._listeners.add(poll);
    poll();
    return () => { cancelled = true; this.db._listeners.delete(poll); };
  }
}

class DocRef {
  constructor(db, path) {
    this.db = db;
    this.path = path;
  }

  collection(subPath) {
    return new CollectionRef(this.db, `${this.path}/${subPath}`);
  }

  async get() {
    const parts = this.path.split('/');

    // Handle admins/{docId}
    if (parts[0] === 'admins') {
      try {
        const r = await this.db._fetch(`/admin/verify/`, 'POST');
        return r.is_admin ? { exists: true, data: () => r } : { exists: false, data: () => null };
      } catch (e) { return { exists: false, data: () => null }; }
    }

    // Handle users/{uid}
    if (parts.length === 2 && parts[0] === 'users') {
      try {
        const data = await this.db._fetch(`/users/${parts[1]}/`);
        return { exists: true, data: () => data };
      } catch (e) { return { exists: false, data: () => null }; }
    }
    
    // Handle users/{uid}/transactions/{id} (unlikely to be used but good for completeness)
    if (parts.length === 4 && parts[0] === 'users' && parts[2] === 'transactions') {
       // Just returning empty/exists:false for now as usually list is used
       return { exists: false, data: () => null };
    }

    return { exists: false, data: () => null };
  }

  async set(data) {
    const parts = this.path.split('/');
    if (parts.length === 2 && parts[0] === 'users') {
      await this.db._fetch(`/users/${parts[1]}/`, 'POST', data);
      this.db._triggerRefresh();
      return;
    }
    throw new Error(`Set not supported for path: ${this.path}`);
  }

  async update(data) {
    const parts = this.path.split('/');
    if (parts.length === 2 && parts[0] === 'users') {
      await this.db._fetch(`/users/${parts[1]}/`, 'PATCH', data);
      this.db._triggerRefresh();
      return;
    }
    if (parts[0] === 'withdrawals') {
      const action = data.status === 'completed' ? 'approve' : 'reject';
      await this.db._fetch(`/admin/withdrawals/${parts[1]}/`, 'PATCH', { action });
      this.db._triggerRefresh();
      return;
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
      } catch (e) { if (!cancelled && errorCallback) errorCallback(e); }
      if (!cancelled) setTimeout(poll, 4000); // 4s poll for profile
    };
    this.db._listeners.add(poll);
    poll();
    return () => { cancelled = true; this.db._listeners.delete(poll); };
  }
}

// Override firebase.firestore
const shim = new FirestoreShim();
firebase.firestore = () => shim;
firebase.firestore.FieldValue = shim.FieldValue;
