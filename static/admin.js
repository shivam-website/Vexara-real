/* ============================================================================
   VEXARA ADMIN PANEL - MAIN JAVASCRIPT
   ============================================================================ */

   const API_BASE = window.location.origin;
   let currentUser = null;
   let usersData = [];
   let usersPage = 1;
   const USERS_PER_PAGE = 10;
   let filteredUsers = [];
   
   // ============================================================================
   // INITIALIZATION
   // ============================================================================
   
   document.addEventListener('DOMContentLoaded', async () => {
       showLoadingOverlay();
       
       // Check authentication
       const authStatus = await checkAdminAuth();
       
       if (!authStatus || !authStatus.authenticated) {
           window.location.href = '/admin/login';
           return;
       }
   
       if (!authStatus.is_admin) {
           showToast('You do not have admin permissions', 'error');
           setTimeout(() => {
               window.location.href = '/';
           }, 2000);
           return;
       }
   
       currentUser = authStatus;
       setupUI();
       setupEventListeners();
       loadDashboard();
       hideLoadingOverlay();
       
       // Update time
       updateTime();
       setInterval(updateTime, 60000);
   });
   
   // ============================================================================
   // AUTHENTICATION
   // ============================================================================
   
   async function checkAdminAuth() {
       try {
           const response = await fetch(`${API_BASE}/admin/auth-check`, {
               credentials: 'include'
           });
           return await response.json();
       } catch (error) {
           console.error('Auth check failed:', error);
           return null;
       }
   }
   
   function setupUI() {
       document.getElementById('adminName').textContent = currentUser.name || currentUser.email.split('@')[0];
       document.getElementById('adminEmail').textContent = currentUser.email;
   }
   
   // ============================================================================
   // EVENT LISTENERS
   // ============================================================================
   
   function setupEventListeners() {
       // Navigation
       document.querySelectorAll('.nav-link').forEach(link => {
           link.addEventListener('click', handleNavigation);
       });
   
       // Sidebar toggle
       document.getElementById('sidebarToggle').addEventListener('click', toggleSidebar);
   
       // Logout
       document.getElementById('logoutBtn').addEventListener('click', handleLogout);
   
       // Users page
       document.getElementById('planFilter').addEventListener('change', filterUsers);
       document.getElementById('sortBy').addEventListener('change', sortUsers);
       document.getElementById('searchInput').addEventListener('input', debounce(searchUsers, 300));
       document.getElementById('prevBtn').addEventListener('click', previousPage);
       document.getElementById('nextBtn').addEventListener('click', nextPage);
   
       // Health page
       document.getElementById('refreshHealthBtn').addEventListener('click', refreshHealth);
       document.getElementById('refreshLogsBtn').addEventListener('click', refreshLogs);
   
       // Modals
       document.getElementById('modalCloseBtn').addEventListener('click', closeUserModal);
       document.getElementById('chatModalCloseBtn').addEventListener('click', closeChatModal);
       document.getElementById('confirmCloseBtn').addEventListener('click', closeConfirmModal);
   
       // User modal actions
       document.getElementById('setFreeBtn').addEventListener('click', () => changePlan('free'));
       document.getElementById('setProBtn').addEventListener('click', () => changePlan('pro'));
       document.getElementById('resetQuotaBtn').addEventListener('click', resetQuota);
       document.getElementById('viewChatsBtn').addEventListener('click', viewUserChats);
       document.getElementById('deleteChatBtn').addEventListener('click', deleteChat);
   }
   
   // ============================================================================
   // NAVIGATION
   // ============================================================================
   
   function handleNavigation(e) {
       e.preventDefault();
       const page = this.dataset.page;
       
       // Update active nav
       document.querySelectorAll('.nav-link').forEach(link => {
           link.classList.remove('active');
       });
       this.classList.add('active');
   
       // Update page title
       const titles = {
           dashboard: 'Dashboard',
           users: 'User Management',
           analytics: 'Analytics',
           health: 'System Health',
           logs: 'Admin Logs'
       };
       document.getElementById('pageTitle').textContent = titles[page] || 'Dashboard';
   
       // Show page
       document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
       document.getElementById(page).classList.add('active');
   
       // Load page data
       switch(page) {
           case 'users':
               loadUsers();
               break;
           case 'analytics':
               loadAnalytics();
               break;
           case 'health':
               loadHealth();
               break;
           case 'logs':
               loadLogs();
               break;
       }
   }
   
   function toggleSidebar() {
       document.querySelector('.sidebar').classList.toggle('active');
   }
   
   // ============================================================================
   // DASHBOARD
   // ============================================================================
   
   async function loadDashboard() {
       try {
           const response = await fetch(`${API_BASE}/admin/analytics/summary`, {
               credentials: 'include'
           });
           
           if (!response.ok) throw new Error('Failed to load analytics');
           
           const data = await response.json();
           
           document.getElementById('totalUsers').textContent = data.total_users;
           document.getElementById('proUsers').textContent = data.pro_users;
           document.getElementById('freeUsers').textContent = data.free_users;
           document.getElementById('messagesToday').textContent = data.messages_today;
           document.getElementById('totalChats').textContent = data.total_chats;
           document.getElementById('conversionRate').textContent = data.conversion_rate;
   
           // Load daily activity
           await loadDailyActivity();
       } catch (error) {
           showToast('Failed to load dashboard: ' + error.message, 'error');
       }
   }
   
   async function loadDailyActivity() {
       try {
           const response = await fetch(`${API_BASE}/admin/analytics/daily`, {
               credentials: 'include'
           });
           
           if (!response.ok) throw new Error('Failed to load daily analytics');
           
           const data = await response.json();
           const dates = Object.keys(data).sort().reverse();
           const messages = dates.map(date => data[date].messages);
           
           drawChart('activityCanvas', dates, messages, 'Messages');
       } catch (error) {
           console.error('Error loading daily activity:', error);
       }
   }
   
   // ============================================================================
   // USERS MANAGEMENT
   // ============================================================================
   
   async function loadUsers() {
       try {
           showLoadingOverlay();
           const response = await fetch(`${API_BASE}/admin/users`, {
               credentials: 'include'
           });
           
           if (!response.ok) throw new Error('Failed to load users');
           
           const data = await response.json();
           usersData = data.users;
           filteredUsers = [...usersData];
           usersPage = 1;
           
           displayUsers();
           hideLoadingOverlay();
       } catch (error) {
           showToast('Failed to load users: ' + error.message, 'error');
           hideLoadingOverlay();
       }
   }
   
   function displayUsers() {
       const start = (usersPage - 1) * USERS_PER_PAGE;
       const end = start + USERS_PER_PAGE;
       const paginatedUsers = filteredUsers.slice(start, end);
   
       const tbody = document.getElementById('usersTableBody');
       tbody.innerHTML = '';
   
       if (paginatedUsers.length === 0) {
           tbody.innerHTML = '<tr class="loading-row"><td colspan="7">No users found</td></tr>';
           document.getElementById('prevBtn').disabled = true;
           document.getElementById('nextBtn').disabled = true;
           return;
       }
   
       paginatedUsers.forEach(user => {
           const row = document.createElement('tr');
           const planBadge = `<span class="plan-badge ${user.plan}">${user.plan.toUpperCase()}</span>`;
           const lastActive = user.last_active ? formatDate(user.last_active) : 'Never';
           
           row.innerHTML = `
               <td>${escapeHtml(user.email)}</td>
               <td>${escapeHtml(user.name)}</td>
               <td>${planBadge}</td>
               <td>${user.messages_today} / 20</td>
               <td>${user.total_chats}</td>
               <td>${lastActive}</td>
               <td class="action-cell">
                   <button class="action-btn view-btn" onclick="openUserModal('${user.user_id}')">
                       <i class="fas fa-eye"></i> View
                   </button>
               </td>
           `;
           tbody.appendChild(row);
       });
   
       // Update pagination
       const totalPages = Math.ceil(filteredUsers.length / USERS_PER_PAGE);
       document.getElementById('pageInfo').textContent = `Page ${usersPage} of ${totalPages}`;
       document.getElementById('prevBtn').disabled = usersPage === 1;
       document.getElementById('nextBtn').disabled = usersPage === totalPages;
   }
   
   function filterUsers() {
       const plan = document.getElementById('planFilter').value;
       filteredUsers = plan ? usersData.filter(u => u.plan === plan) : [...usersData];
       usersPage = 1;
       displayUsers();
   }
   
   function sortUsers() {
       const sortBy = document.getElementById('sortBy').value;
       
       if (sortBy === 'recent') {
           filteredUsers.sort((a, b) => new Date(b.last_active) - new Date(a.last_active));
       } else if (sortBy === 'name') {
           filteredUsers.sort((a, b) => a.name.localeCompare(b.name));
       } else if (sortBy === 'messages') {
           filteredUsers.sort((a, b) => b.messages_today - a.messages_today);
       }
       
       usersPage = 1;
       displayUsers();
   }
   
   function searchUsers(query) {
       const term = query.toLowerCase();
       filteredUsers = usersData.filter(user => 
           user.email.toLowerCase().includes(term) ||
           user.name.toLowerCase().includes(term)
       );
       usersPage = 1;
       displayUsers();
   }
   
   function previousPage() {
       if (usersPage > 1) {
           usersPage--;
           displayUsers();
           window.scrollTo({ top: 0, behavior: 'smooth' });
       }
   }
   
   function nextPage() {
       const totalPages = Math.ceil(filteredUsers.length / USERS_PER_PAGE);
       if (usersPage < totalPages) {
           usersPage++;
           displayUsers();
           window.scrollTo({ top: 0, behavior: 'smooth' });
       }
   }
   
   // ============================================================================
   // USER DETAIL MODAL
   // ============================================================================
   
   let currentModalUserId = null;
   
   async function openUserModal(userId) {
       currentModalUserId = userId;
       
       try {
           showLoadingOverlay();
           const response = await fetch(`${API_BASE}/admin/user/${userId}`, {
               credentials: 'include'
           });
           
           if (!response.ok) throw new Error('Failed to load user details');
           
           const user = await response.json();
           
           document.getElementById('modalUserEmail').textContent = user.email;
           document.getElementById('modalEmail').textContent = user.email;
           document.getElementById('modalName').textContent = user.name;
           document.getElementById('modalPlan').innerHTML = `<span class="plan-badge ${user.plan}">${user.plan.toUpperCase()}</span>`;
           document.getElementById('modalCreated').textContent = formatDate(user.created_at);
           document.getElementById('modalMessagesToday').textContent = `${user.quota.today} / ${user.quota.daily_limit}`;
           document.getElementById('modalTotalChats').textContent = user.chats.total;
           
           // Hide/show chat list
           const chatsList = document.getElementById('chatsList');
           if (user.chats.total === 0) {
               chatsList.style.display = 'none';
           } else {
               chatsList.style.display = 'block';
           }
           
           document.getElementById('userModal').classList.add('active');
           hideLoadingOverlay();
       } catch (error) {
           showToast('Failed to load user details: ' + error.message, 'error');
           hideLoadingOverlay();
       }
   }
   
   function closeUserModal() {
       document.getElementById('userModal').classList.remove('active');
       currentModalUserId = null;
       document.getElementById('chatsList').style.display = 'none';
   }
   
   async function changePlan(newPlan) {
       if (!currentModalUserId) return;
       
       const currentPlan = document.getElementById('modalPlan').textContent.toLowerCase();
       if (currentPlan === newPlan) {
           showToast(`User is already on ${newPlan} plan`, 'warning');
           return;
       }
   
       try {
           showConfirm(
               `Are you sure you want to change this user to ${newPlan.toUpperCase()} plan?`,
               async () => {
                   showLoadingOverlay();
                   const response = await fetch(`${API_BASE}/admin/user/${currentModalUserId}/plan`, {
                       method: 'POST',
                       credentials: 'include',
                       headers: { 'Content-Type': 'application/json' },
                       body: JSON.stringify({ plan: newPlan })
                   });
                   
                   if (!response.ok) throw new Error('Failed to update plan');
                   
                   showToast(`User plan updated to ${newPlan}`, 'success');
                   document.getElementById('modalPlan').innerHTML = `<span class="plan-badge ${newPlan}">${newPlan.toUpperCase()}</span>`;
                   await loadUsers(); // Refresh user list
                   hideLoadingOverlay();
               }
           );
       } catch (error) {
           showToast('Error: ' + error.message, 'error');
           hideLoadingOverlay();
       }
   }
   
   async function resetQuota() {
       if (!currentModalUserId) return;
       
       try {
           showConfirm(
               'Reset this user\'s message quota for today?',
               async () => {
                   showLoadingOverlay();
                   const response = await fetch(`${API_BASE}/admin/user/${currentModalUserId}/quota/reset`, {
                       method: 'POST',
                       credentials: 'include'
                   });
                   
                   if (!response.ok) throw new Error('Failed to reset quota');
                   
                   showToast('User quota reset successfully', 'success');
                   document.getElementById('modalMessagesToday').textContent = '0 / 20';
                   hideLoadingOverlay();
               }
           );
       } catch (error) {
           showToast('Error: ' + error.message, 'error');
           hideLoadingOverlay();
       }
   }
   
   async function viewUserChats() {
       if (!currentModalUserId) return;
       
       try {
           showLoadingOverlay();
           const response = await fetch(`${API_BASE}/admin/user/${currentModalUserId}/chats`, {
               credentials: 'include'
           });
           
           if (!response.ok) throw new Error('Failed to load chats');
           
           const data = await response.json();
           const container = document.getElementById('chatsContainer');
           
           if (data.chats.length === 0) {
               container.innerHTML = '<p class="loading">No chats found</p>';
               hideLoadingOverlay();
               return;
           }
           
           container.innerHTML = data.chats.map(chat => `
               <div class="chat-item" onclick="openChatModal('${currentModalUserId}', '${chat.chat_id}')">
                   <div class="chat-item-title">${escapeHtml(chat.title)}</div>
                   <div class="chat-item-meta">
                       Messages: ${chat.message_count} | Created: ${formatDate(chat.created_at)}
                   </div>
               </div>
           `).join('');
           
           hideLoadingOverlay();
       } catch (error) {
           showToast('Failed to load chats: ' + error.message, 'error');
           hideLoadingOverlay();
       }
   }
   
   // ============================================================================
   // CHAT DETAIL MODAL
   // ============================================================================
   
   let currentChatUserId = null;
   let currentChatId = null;
   
   async function openChatModal(userId, chatId) {
       currentChatUserId = userId;
       currentChatId = chatId;
       
       try {
           showLoadingOverlay();
           const response = await fetch(`${API_BASE}/admin/user/${userId}/chat/${chatId}`, {
               credentials: 'include'
           });
           
           if (!response.ok) throw new Error('Failed to load chat');
           
           const chat = await response.json();
           
           document.getElementById('chatModalTitle').textContent = `Chat: ${chat.title}`;
           
           const detailDiv = document.getElementById('chatDetail');
           detailDiv.innerHTML = `
               <div class="chat-detail-header">
                   <h3>${escapeHtml(chat.title)}</h3>
                   <p>Created: ${formatDate(chat.created_at)}</p>
                   <p>Total Messages: ${chat.total_messages}</p>
               </div>
               <div class="chat-messages" style="max-height: 400px; overflow-y: auto;">
                   ${chat.messages.map(msg => `
                       <div class="message-item" style="margin: 10px 0; padding: 10px; background: ${msg.role === 'user' ? '#f3f4f6' : '#e5e7eb'}; border-radius: 6px;">
                           <strong>${msg.role === 'user' ? 'User' : 'AI'}:</strong>
                           <p>${escapeHtml(msg.content)}</p>
                           <small>${formatDate(msg.timestamp)}</small>
                       </div>
                   `).join('')}
               </div>
           `;
           
           document.getElementById('chatModal').classList.add('active');
           hideLoadingOverlay();
       } catch (error) {
           showToast('Failed to load chat details: ' + error.message, 'error');
           hideLoadingOverlay();
       }
   }
   
   function closeChatModal() {
       document.getElementById('chatModal').classList.remove('active');
       currentChatUserId = null;
       currentChatId = null;
   }
   
   async function deleteChat() {
       if (!currentChatUserId || !currentChatId) return;
       
       showConfirm(
           'Are you sure you want to delete this chat? This action cannot be undone.',
           async () => {
               try {
                   showLoadingOverlay();
                   const response = await fetch(
                       `${API_BASE}/admin/user/${currentChatUserId}/chat/${currentChatId}/delete`,
                       {
                           method: 'DELETE',
                           credentials: 'include'
                       }
                   );
                   
                   if (!response.ok) throw new Error('Failed to delete chat');
                   
                   showToast('Chat deleted successfully', 'success');
                   closeChatModal();
                   await viewUserChats();
                   hideLoadingOverlay();
               } catch (error) {
                   showToast('Error: ' + error.message, 'error');
                   hideLoadingOverlay();
               }
           }
       );
   }
   
   // ============================================================================
   // ANALYTICS
   // ============================================================================
   
   async function loadAnalytics() {
       try {
           showLoadingOverlay();
           
           // Get summary data
           const response = await fetch(`${API_BASE}/admin/analytics/summary`, {
               credentials: 'include'
           });
           
           if (!response.ok) throw new Error('Failed to load analytics');
           
           const summary = await response.json();
           
           // Draw plan distribution
           const planCtx = document.getElementById('planChart').getContext('2d');
           new Chart(planCtx, {
               type: 'doughnut',
               data: {
                   labels: ['Free', 'Pro'],
                   datasets: [{
                       data: [summary.free_users, summary.pro_users],
                       backgroundColor: ['#d1d5db', '#a78bfa'],
                       borderColor: ['#fff', '#fff'],
                       borderWidth: 2
                   }]
               },
               options: {
                   responsive: true,
                   plugins: {
                       legend: {
                           position: 'bottom'
                       }
                   }
               }
           });
           
           hideLoadingOverlay();
       } catch (error) {
           showToast('Failed to load analytics: ' + error.message, 'error');
           hideLoadingOverlay();
       }
   }
   
   // ============================================================================
   // HEALTH CHECK
   // ============================================================================
   
   async function loadHealth() {
       await refreshHealth();
   }
   
   async function refreshHealth() {
       try {
           const btn = document.getElementById('refreshHealthBtn');
           btn.classList.add('loading');
           
           const response = await fetch(`${API_BASE}/admin/health`, {
               credentials: 'include'
           });
           
           if (!response.ok) throw new Error('Failed to load health');
           
           const health = await response.json();
           
           const container = document.getElementById('healthItems');
           let html = '';
           
           // Firebase status
           const firebaseStatus = health.firebase.status === 'connected' ? 'connected' : 'error';
           html += `
               <div class="health-item">
                   <div class="health-item-info">
                       <h4>Firebase</h4>
                       <p>${health.firebase.status}</p>
                   </div>
                   <div class="health-status">
                       <div class="status-indicator ${firebaseStatus}"></div>
                       <span>${health.firebase.status.toUpperCase()}</span>
                   </div>
               </div>
           `;
           
           // API Providers
           for (const [provider, status] of Object.entries(health.api_providers)) {
               const isWorking = typeof status === 'string' && status.includes('working');
               const statusClass = isWorking ? 'connected' : 'error';
               html += `
                   <div class="health-item">
                       <div class="health-item-info">
                           <h4>${provider}</h4>
                           <p>${status}</p>
                       </div>
                       <div class="health-status">
                           <div class="status-indicator ${statusClass}"></div>
                           <span>${isWorking ? 'WORKING' : 'ERROR'}</span>
                       </div>
                   </div>
               `;
           }
           
           // Storage
           html += `
               <div class="health-item">
                   <div class="health-item-info">
                       <h4>Local Storage</h4>
                       <p>${health.storage.quota_file ? 'Available' : 'Not Found'}</p>
                   </div>
                   <div class="health-status">
                       <div class="status-indicator ${health.storage.quota_file ? 'connected' : 'error'}"></div>
                       <span>${health.storage.quota_file ? 'OK' : 'MISSING'}</span>
                   </div>
               </div>
           `;
           
           container.innerHTML = html;
           btn.classList.remove('loading');
           showToast('Health check completed', 'success');
       } catch (error) {
           showToast('Health check failed: ' + error.message, 'error');
           document.getElementById('refreshHealthBtn').classList.remove('loading');
       }
   }
   
   // ============================================================================
   // LOGS
   // ============================================================================
   
   async function loadLogs() {
       await refreshLogs();
   }
   
   async function refreshLogs() {
       try {
           const btn = document.getElementById('refreshLogsBtn');
           btn.classList.add('loading');
           
           const response = await fetch(`${API_BASE}/admin/logs`, {
               credentials: 'include'
           });
           
           if (!response.ok) throw new Error('Failed to load logs');
           
           const data = await response.json();
           const tbody = document.getElementById('logsTableBody');
           
           if (data.logs.length === 0) {
               tbody.innerHTML = '<tr class="loading-row"><td colspan="5">No logs found</td></tr>';
               btn.classList.remove('loading');
               return;
           }
           
           tbody.innerHTML = data.logs.map(log => `
               <tr>
                   <td>${formatDate(log.timestamp)}</td>
                   <td>${escapeHtml(log.admin_email || 'System')}</td>
                   <td>${escapeHtml(log.action)}</td>
                   <td>${escapeHtml(log.target_user || '-')}</td>
                   <td>${JSON.stringify(log.details || {}).substring(0, 50)}...</td>
               </tr>
           `).join('');
           
           btn.classList.remove('loading');
           showToast('Logs refreshed', 'success');
       } catch (error) {
           showToast('Failed to load logs: ' + error.message, 'error');
           document.getElementById('refreshLogsBtn').classList.remove('loading');
       }
   }
   
   // ============================================================================
   // UTILITIES
   // ============================================================================
   
   function formatDate(dateString) {
       if (!dateString) return '-';
       const date = new Date(dateString);
       return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { 
           hour: '2-digit', 
           minute: '2-digit' 
       });
   }
   
   function escapeHtml(text) {
       const map = {
           '&': '&amp;',
           '<': '&lt;',
           '>': '&gt;',
           '"': '&quot;',
           "'": '&#039;'
       };
       return text.replace(/[&<>"']/g, m => map[m]);
   }
   
   function debounce(func, wait) {
       let timeout;
       return function executedFunction(...args) {
           const later = () => {
               clearTimeout(timeout);
               func(...args);
           };
           clearTimeout(timeout);
           timeout = setTimeout(later, wait);
       };
   }
   
   function updateTime() {
       const now = new Date();
       const timeString = now.toLocaleTimeString([], { 
           hour: '2-digit', 
           minute: '2-digit',
           second: '2-digit'
       });
       document.getElementById('currentTime').textContent = timeString;
   }
   
   // ============================================================================
   // MODALS & NOTIFICATIONS
   // ============================================================================
   
   function showToast(message, type = 'info') {
       const toast = document.getElementById('toast');
       toast.textContent = message;
       toast.className = `toast ${type} show`;
       
       setTimeout(() => {
           toast.classList.remove('show');
       }, 4000);
   }
   
   function showLoadingOverlay() {
       document.getElementById('loadingOverlay').classList.remove('hidden');
   }
   
   function hideLoadingOverlay() {
       document.getElementById('loadingOverlay').classList.add('hidden');
   }
   
   function showConfirm(message, onConfirm) {
       document.getElementById('confirmMessage').textContent = message;
       document.getElementById('confirmModal').classList.add('active');
       
       const yesBtn = document.getElementById('confirmYesBtn');
       const cancelBtn = document.getElementById('confirmCancelBtn');
       
       yesBtn.onclick = () => {
           closeConfirmModal();
           onConfirm();
       };
       
       cancelBtn.onclick = closeConfirmModal;
   }
   
   function closeConfirmModal() {
       document.getElementById('confirmModal').classList.remove('active');
   }
   
   // ============================================================================
   // CHARTS
   // ============================================================================
   
   function drawChart(canvasId, labels, data, label) {
       const ctx = document.getElementById(canvasId);
       if (!ctx) return;
       
       const chartContext = ctx.getContext('2d');
       new Chart(chartContext, {
           type: 'line',
           data: {
               labels: labels,
               datasets: [{
                   label: label,
                   data: data,
                   borderColor: '#6366f1',
                   backgroundColor: 'rgba(99, 102, 241, 0.1)',
                   tension: 0.4,
                   fill: true,
                   pointRadius: 5,
                   pointBackgroundColor: '#6366f1',
                   pointBorderColor: '#fff',
                   pointBorderWidth: 2
               }]
           },
           options: {
               responsive: true,
               maintainAspectRatio: false,
               plugins: {
                   legend: {
                       display: true,
                       position: 'top'
                   }
               },
               scales: {
                   y: {
                       beginAtZero: true,
                       ticks: {
                           stepSize: 1
                       }
                   }
               }
           }
       });
   }
   
   // ============================================================================
   // LOGOUT
   // ============================================================================
   
   async function handleLogout() {
       try {
           await fetch(`${API_BASE}/admin/logout`, {
               method: 'POST',
               credentials: 'include'
           });
           
           showToast('Logged out successfully', 'success');
           setTimeout(() => {
               window.location.href = '/';
           }, 1000);
       } catch (error) {
           console.error('Logout error:', error);
           window.location.href = '/';
       }
   }