const fs = require("fs");
const path = require("path");
const version = "2022";
let vsPath = process.env[`vs${version}_install`];
console.log("vsPath:", vsPath);
console.log("exists:", fs.existsSync(vsPath));
if (vsPath && fs.existsSync(vsPath)) {
    console.log("inside if");
} else {
    console.log("outside if");
}
