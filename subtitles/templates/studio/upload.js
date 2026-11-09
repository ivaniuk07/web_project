(() => {
    const form = document.getElementById('upload');
    const input = form.querySelector('input[type="file"]');
    const selected = document.getElementById('selected-video');

    const showSelection = () => {
        selected.textContent = input.files.length ? input.files[0].name : '';
    };

    input.addEventListener('change', showSelection);
    form.addEventListener('dragover', event => {
        if (!event.dataTransfer.types.includes('Files')) return;
        event.preventDefault();
        form.classList.add('is-dragging');
    });
    form.addEventListener('dragleave', event => {
        if (!form.contains(event.relatedTarget)) form.classList.remove('is-dragging');
    });
    form.addEventListener('drop', event => {
        event.preventDefault();
        form.classList.remove('is-dragging');
        if (event.dataTransfer.files.length !== 1) {
            selected.textContent = 'Оберіть один відеофайл.';
            return;
        }
        input.files = event.dataTransfer.files;
        showSelection();
    });
})();
