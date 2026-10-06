import {defineConfig} from 'vite';
export default defineConfig({server:{host:'127.0.0.1',port:8503,strictPort:true,proxy:{'/api':'http://127.0.0.1:8001','/health':'http://127.0.0.1:8001'}}});
