// loadData.js
fetch('yourfile.json')
  .then(response => response.json())
  .then(data => {
    console.log(data);
    // Use the JSON data here or call other functions
  })
  .catch(error => console.error('Error loading JSON:', error));

