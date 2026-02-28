let currentData = {};
let backupData = {};

function createGroupEditor(groupId, userIds) {
    return `
        <div class="group-section">
            <div class="group-header">
                <input 
                    type="text" 
                    value="${groupId}" 
                    placeholder="群号码" 
                    class="input-field group-id"
                >
                <button class="btn delete-btn" onclick="deleteGroup(this)">🗑️ 删除群组</button>
            </div>
            <div class="user-list">
                ${userIds.map(uid => `
                    <div class="user-item">
                        <input 
                            type="text" 
                            value="${uid}" 
                            placeholder="用户ID" 
                            class="input-field user-id"
                        >
                        <button class="btn delete-btn" onclick="deleteUser(this)">✖️ 删除</button>
                    </div>
                `).join('')}
            </div>
            <button class="btn" onclick="addUserField(this)">➕ 添加用户</button>
        </div>
    `;
}

function updateEditor() {
    document.getElementById('editor').innerHTML = Object.entries(currentData)
        .map(([k,v]) => createGroupEditor(k, v))
        .join('');
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast-message toast-${type}`;
    toast.textContent = message;
    
    container.appendChild(toast);
    // 触发动画
    setTimeout(() => toast.classList.add('toast-visible'), 10);
    
    // 自动消失
    setTimeout(() => {
        toast.classList.remove('toast-visible');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

async function loadConfig() {
    try {
        const res = await fetch('/get_config');
        currentData = await res.json();
        backupData = JSON.parse(JSON.stringify(currentData));
        updateEditor();
        showToast('🟢 已加载配置', 'info');
    } catch (error) {
        console.error('加载配置失败:', error);
        showToast('⛔ 撤销失败', 'error');
    }
}

async function saveConfig() {
    try {
        const newData = {};

        document.querySelectorAll('.group-section').forEach(section => {
            const groupId = section.querySelector('.group-id').value;
            // 过滤空群号
            if (!groupId) return;
    
            newData[groupId] = Array.from(section.querySelectorAll('.user-id'))
                .map(input => input.value.trim())
                .filter(v => v);
        });
    
        // 更新当前数据
        currentData = newData;
        
        const response = await fetch('/save_config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(newData)
        });
        loadConfig(); // 重新加载确保数据同步
        
        const result = await response.json();
        if (result.status === 'success') {
            showToast('✅ 保存成功', 'success');
        } else {
            console.error('保存失败:', result.message);
            showToast('⛔ 保存失败，请检查控制台输出', 'error');
        }
    } catch (error) {
        console.error('保存请求异常:', error);
        showToast('⛔ 保存失败，请检查控制台输出', 'error');
    }
}


function addNewGroup() {
    const tempId = `new_group_${Date.now()}`;
    currentData[tempId] = [];
    updateEditor();
}

function addUserField(btn) {
    const userItem = document.createElement('div');
    userItem.className = 'user-item';
    userItem.innerHTML = `
        <input type="text" placeholder="用户ID" class="input-field user-id">
        <button class="btn delete-btn" onclick="deleteUser(this)">✖️ 删除</button>
    `;
    btn.before(userItem);
}

function deleteUser(btn) {
    btn.closest('div').remove();
}

function deleteGroup(btn) {
    const section = btn.closest('.group-section');
    const groupIdInput = section.querySelector('.group-id');
    const currentGroupId = groupIdInput.value;
    
    // 同步删除数据
    delete currentData[currentGroupId];
    section.remove();
}

// 初始化加载
loadConfig();