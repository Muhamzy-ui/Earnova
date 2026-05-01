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
      return this._fixTimestamps(result);
    } catch (err) {
      console.error(`Fetch error for ${endpoint}:`, err);
      throw err;
    }
  }

  _fixTimestamps(obj) {
    if (!obj || typeof obj !== 'object') return obj;
    if (Array.isArray(obj)) return obj.map(item => this._fixTimestamps(item));
    
    // Check if it looks like a Firebase timestamp object sent as JSON from Django
    if (obj.toDate && typeof obj.toDate === 'string' && (obj._seconds || obj.seconds)) {
      const dateStr = obj.toDate;
      obj.toDate = function() { return new Date(dateStr); };
      if (!obj.seconds) obj.seconds = obj._seconds;
    }
    
    // Recurse into properties
    for (const key in obj) {
      if (Object.prototype.hasOwnProperty.call(obj, key) && key !== 'toDate') {
        obj[key] = this._fixTimestamps(obj[key]);
      }
    }
    return obj;
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
      let queryUrl = `/users/${parts[1]}/transactions/?limit=${this._limit}`;
      const typeFilter = this.filters.find(f => f.field === 'type' && f.op === '==');
      if (typeFilter) {
        queryUrl += `&type=${typeFilter.value}`;
      }
      const results = await this.db._fetch(queryUrl);
      return this._wrapDocs(results);
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
    const docs = list.map(d => {
      const id = d.uid || d.doc_id || d.id || 'unknown';
      const isUsers = this.path === 'users';
      const docPath = isUsers ? `users/${id}` : `${this.path}/${id}`;
      return {
        id,
        data: () => d,
        exists: true,
        ref: new DocRef(this.db, docPath)
      };
    });
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
      if (!cancelled) setTimeout(poll, 5000); // Poll every 5s for referral list
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
    
    // Handle users/{uid}/transactions/{id}
    if (parts.length === 4 && parts[0] === 'users' && parts[2] === 'transactions') {
      try {
        const data = await this.db._fetch(`/users/${parts[1]}/transactions/${parts[3]}/`);
        return { exists: true, data: () => data };
      } catch (e) { return { exists: false, data: () => null }; }
    }

    // Handle settings/{docId}
    if (parts.length === 2 && parts[0] === 'settings') {
      try {
        const r = await this.db._fetch(`/settings/${parts[1]}/`);
        if (r && r.exists) return { exists: true, data: () => r.data };
        return { exists: false, data: () => ({}) };
      } catch (e) { return { exists: false, data: () => ({}) }; }
    }

    return { exists: false, data: () => null };
  }

  async set(data, options = {}) {
    const parts = this.path.split('/');
    const method = (options.merge) ? 'PATCH' : 'POST';

    // Handle users/{uid}
    if (parts.length === 2 && parts[0] === 'users') {
      await this.db._fetch(`/users/${parts[1]}/`, method, data);
      this.db._triggerRefresh();
      return;
    }

    // Handle settings/{docId}
    if (parts.length === 2 && parts[0] === 'settings') {
      await this.db._fetch(`/settings/${parts[1]}/`, method, data);
      return;
    }

    // Handle withdrawals/{docId}
    if (parts.length === 2 && parts[0] === 'withdrawals') {
      await this.db._fetch(`/withdrawals/${parts[1]}/`, method, data);
      this.db._triggerRefresh();
      return;
    }

    // Handle users/{uid}/transactions/{txnId}
    if (parts.length === 4 && parts[0] === 'users' && parts[2] === 'transactions') {
      const uid = parts[1];
      const txnId = parts[3];
      
      if (options.merge) {
        // Use the specific document endpoint for updates
        await this.db._fetch(`/users/${uid}/transactions/${txnId}/`, 'PATCH', data);
      } else {
        // Use the collection endpoint for creation
        const payload = { ...data, id: txnId, doc_id: txnId };
        await this.db._fetch(`/users/${uid}/transactions/`, 'POST', payload);
      }
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
    if (parts.length === 2 && parts[0] === 'settings') {
      await this.db._fetch(`/settings/${parts[1]}/`, 'PATCH', data);
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

  async delete() {
    const parts = this.path.split('/');
    // users/{uid} — ban/delete user (mark as banned in backend)
    if (parts.length === 2 && parts[0] === 'users') {
      await this.db._fetch(`/users/${parts[1]}/`, 'PATCH', { status: 'banned' });
      this.db._triggerRefresh();
      return;
    }
    // withdrawals/{docId}
    if (parts.length === 2 && parts[0] === 'withdrawals') {
      await this.db._fetch(`/admin/withdrawals/${parts[1]}/`, 'PATCH', { action: 'reject' });
      this.db._triggerRefresh();
      return;
    }
    // For anything else, silently succeed (no-op) to avoid crashes
    console.warn(`Delete silently ignored for path: ${this.path}`);
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
